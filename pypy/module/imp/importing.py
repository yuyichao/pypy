"""
Implementation of the interpreter-level default import logic.
"""

import sys, os, stat

from pypy.interpreter.module import Module
from pypy.interpreter.gateway import interp2app, unwrap_spec
from pypy.interpreter.typedef import TypeDef, generic_new_descr
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.baseobjspace import W_Root, CannotHaveLock
from pypy.interpreter.eval import Code
from pypy.interpreter.pycode import PyCode
from rpython.rlib import streamio, jit
from rpython.rlib.streamio import StreamErrors
from rpython.rlib.objectmodel import we_are_translated, specialize
from rpython.rlib.signature import signature
from rpython.rlib import rposix, types, rstring
from pypy.module.sys.version import PYPY_VERSION

_WIN32 = sys.platform == 'win32'

SEARCH_ERROR = 0
PY_SOURCE = 1
PY_COMPILED = 2
C_EXTENSION = 3
# PY_RESOURCE = 4
PKG_DIRECTORY = 5
C_BUILTIN = 6
PY_FROZEN = 7
# PY_CODERESOURCE = 8
IMP_HOOK = 9

SO = '.pyd' if _WIN32 else '.so'
PREFIX = 'pypy3-'
DEFAULT_SOABI = '%s%d%d' % ((PREFIX,) + PYPY_VERSION[:2])

PYC_TAG = '%s%d%d' % ((PREFIX,) + PYPY_VERSION[:2])

@specialize.memo()
def get_so_extension(space):
    if space.config.objspace.soabi is not None:
        soabi = space.config.objspace.soabi
    else:
        soabi = DEFAULT_SOABI
    rstring.check_ascii(soabi)

    if not soabi:
        return SO

    if not space.config.translating:
        soabi += 'i'

    return '.' + soabi + SO

def fsdecode(space, s):
    assert isinstance(s, bytes)
    try:
        # ascii encoding works at initialization time
        return space.wrap(s.decode('ascii'))
    except UnicodeDecodeError:
        return space.fsdecode(space.wrapbytes(s))

def fsdecode_w(space, s):
    assert isinstance(s, bytes)
    return space.unicode0_w(space.fsdecode(space.wrapbytes(s)))

def file_exists(path):
    """Tests whether the given path is an existing regular file."""
    return os.path.isfile(path) and case_ok(path)

def find_modtype(space, filepart):
    """Check which kind of module to import for the given filepart,
    which is a path without extension.  Returns PY_SOURCE, PY_COMPILED or
    SEARCH_ERROR.
    """
    # check the .py file
    pyfile = filepart + ".py"
    if file_exists(pyfile):
        return PY_SOURCE, ".py", "U"

    # on Windows, also check for a .pyw file
    if _WIN32:
        pyfile = filepart + ".pyw"
        if file_exists(pyfile):
            return PY_SOURCE, ".pyw", "U"

    # The .py file does not exist, check the .pyc file
    if space.config.objspace.usepycfiles:
        pycfile = filepart + ".pyc"
        if file_exists(pycfile):
            # existing .pyc file
            return PY_COMPILED, ".pyc", "rb"

    if space.config.objspace.usemodules.cpyext:
        so_extension = get_so_extension(space)
        pydfile = filepart + so_extension
        if file_exists(pydfile):
            return C_EXTENSION, so_extension, "rb"

    return SEARCH_ERROR, None, None

if sys.platform.startswith('linux') or 'freebsd' in sys.platform:
    def case_ok(filename):
        assert isinstance(filename, str)
        return True
else:
    # XXX that's slow
    def case_ok(filename):
        assert isinstance(filename, str)
        index = rightmost_sep(filename)
        if index < 0:
            directory = os.curdir
        else:
            directory = filename[:index + 1]
            filename = filename[index + 1:]
        try:
            return filename in os.listdir(directory)
        except OSError:
            return False

def try_getattr(space, w_obj, w_name):
    try:
        return space.getattr(w_obj, w_name)
    except OperationError:
        # ugh, but blame CPython :-/ this is supposed to emulate
        # hasattr, which eats all exceptions.
        return None

def check_sys_modules(space, w_modulename):
    return space.finditem(space.sys.get('modules'), w_modulename)

def check_sys_modules_w(space, modulename):
    rstring.check_utf8(modulename)
    return space.finditem(space.sys.get('modules'),
                          space.wrap(modulename.decode('utf-8')))

@jit.elidable
def _get_dot_position(str, n):
    # return the index in str of the '.' such that there are n '.'-separated
    # strings after it
    rstring.check_utf8(str)
    result = len(str)
    while n > 0 and result >= 0:
        n -= 1
        result = str.rfind('.', 0, result)
    return result

def _convert_utf8(space, w_value):
    if space.isinstance_w(w_value, space.w_unicode):
        try:
            u_value = space.unicode_w(w_value)
            value = u_value.encode('utf-8')
            return value, rstring.assert_utf8(u_value)
        except UnicodeEncodeError:
            pass
    return None, None

def _convert_unicode_attr(space, w_value, name):
    value, u_value = _convert_utf8(space, w_value)
    if value is not None and '\x00' not in value:
        return rstring.assert_str0(value), rstring.assert_str0(u_value)
    raise OperationError(space.w_ValueError,
                         space.wrap(u"%s set to non-string" % name))

def _get_relative_name(space, modulename, level, w_globals):
    rstring.check_utf8(modulename)

    w = space.wrap
    ctxt_w_package = space.finditem_str(w_globals, '__package__')
    ctxt_w_package = jit.promote(ctxt_w_package)
    level = jit.promote(level)

    ctxt_package = None
    if ctxt_w_package is not None and ctxt_w_package is not space.w_None:
        ctxt_package, ctxt_u_package = _convert_unicode_attr(
            space, ctxt_w_package, u'__package__')

    if ctxt_package is not None:
        # __package__ is set, so use it
        if ctxt_package == '' and level < 0:
            return None, 0

        dot_position = _get_dot_position(ctxt_package, level - 1)
        if dot_position < 0:
            if len(ctxt_package) == 0:
                msg = u"Attempted relative import in non-package"
            else:
                msg = u"Attempted relative import beyond toplevel package"
            raise OperationError(space.w_ValueError, w(msg))

        # Try to import parent package
        try:
            absolute_import(space, ctxt_package, 0, None, tentative=False)
        except OperationError, e:
            if not e.match(space, space.w_ImportError):
                raise
            if level > 0:
                msg = (u"Parent module '%s' not loaded, "
                       "cannot perform relative import" % ctxt_u_package)
                raise OperationError(space.w_SystemError, w(msg))
            else:
                msg = (u"Parent module '%s' not found while handling absolute "
                       "import" % ctxt_u_package)
                space.warn(w(msg), space.w_RuntimeWarning)

        rel_modulename = rstring.assert_utf8(ctxt_package[:dot_position])
        rel_level = rel_modulename.count('.') + 1
        if modulename:
            rel_modulename += '.' + modulename
    else:
        # __package__ not set, so figure it out and set it
        ctxt_w_name = space.finditem_str(w_globals, '__name__')
        ctxt_w_path = space.finditem_str(w_globals, '__path__')

        ctxt_w_name = jit.promote(ctxt_w_name)
        ctxt_name = None
        if ctxt_w_name is not None:
            ctxt_name, ctxt_u_name = _convert_unicode_attr(
                space, ctxt_w_name, u'__name__')

        if not ctxt_name:
            return None, 0

        m = max(level - 1, 0)
        if ctxt_w_path is None:   # plain module
            m += 1
        dot_position = _get_dot_position(ctxt_name, m)
        if dot_position < 0:
            if level > 0:
                msg = u"Attempted relative import in non-package"
                raise OperationError(space.w_ValueError, w(msg))
            rel_modulename = ''
            rel_level = 0
        else:
            rel_modulename = rstring.assert_utf8(ctxt_name[:dot_position])
            rel_level = rel_modulename.count('.') + 1

        if ctxt_w_path is not None:
            # __path__ is set, so __name__ is already the package name
            space.setitem(w_globals, w(u"__package__"), ctxt_w_name)
        else:
            # Normal module, so work out the package name if any
            last_dot_position = ctxt_name.rfind('.')
            if last_dot_position < 0:
                space.setitem(w_globals, w(u"__package__"), space.w_None)
            else:
                space.setitem(w_globals, w(u"__package__"),
                              w(ctxt_name[:last_dot_position].decode('utf-8')))

        if modulename:
            if rel_modulename:
                rel_modulename += '.' + modulename
            else:
                rel_modulename = modulename

    rstring.check_utf8(rel_modulename)
    return rel_modulename, rel_level


@unwrap_spec(name='unicode0', level=int)
def importhook(space, name, w_globals=None, w_locals=None,
               w_fromlist=None, level=-1):
    w = space.wrap
    try:
        modulename = name.encode('utf-8')
    except UnicodeEncodeError:
        raise OperationError(space.w_ValueError, w(u"Invalid module name"))
    if not modulename and level < 0:
        raise OperationError(space.w_ValueError, w(u"Empty module name"))

    if w_fromlist is not None and space.is_true(w_fromlist):
        fromlist_w = space.fixedview(w_fromlist)
    else:
        fromlist_w = None

    rel_modulename = None
    if (level != 0 and w_globals is not None and
            space.isinstance_w(w_globals, space.w_dict)):
        rel_modulename, rel_level = _get_relative_name(space, modulename, level,
                                                       w_globals)
        if rel_modulename:
            # if no level was set, ignore import errors, and
            # fall back to absolute import at the end of the
            # function.
            if level == -1:
                # This check is a fast path to avoid redoing the
                # following absolute_import() in the common case
                w_mod = check_sys_modules_w(space, rel_modulename)
                if w_mod is not None and space.is_w(w_mod, space.w_None):
                    # if we already find space.w_None, it means that we
                    # already tried and failed and fell back to the
                    # end of this function.
                    w_mod = None
                else:
                    w_mod = absolute_import(space, rel_modulename, rel_level,
                                            fromlist_w, tentative=True)
            else:
                w_mod = absolute_import(space, rel_modulename, rel_level,
                                        fromlist_w, tentative=False)
            if w_mod is not None:
                return w_mod

    w_mod = absolute_import(space, modulename, 0, fromlist_w, tentative=0)
    if rel_modulename is not None:
        rstring.check_utf8(rel_modulename)
        space.setitem(space.sys.get('modules'),
                      w(rel_modulename.decode('utf-8')), space.w_None)
    return w_mod

def absolute_import(space, modulename, baselevel, fromlist_w, tentative):
    # Short path: check in sys.modules, but only if there is no conflict
    # on the import lock.  In the situation of 'import' statements
    # inside tight loops, this should be true, and absolute_import_try()
    # should be followed by the JIT and turned into not much code.  But
    # if the import lock is currently held by another thread, then we
    # have to wait, and so shouldn't use the fast path.
    rstring.check_utf8(modulename)
    if not getimportlock(space).lock_held_by_someone_else():
        w_mod = absolute_import_try(space, modulename, baselevel, fromlist_w)
        if w_mod is not None and not space.is_w(w_mod, space.w_None):
            return w_mod
    return absolute_import_with_lock(space, modulename, baselevel,
                                     fromlist_w, tentative)

@jit.dont_look_inside
def absolute_import_with_lock(space, modulename, baselevel,
                              fromlist_w, tentative):
    lock = getimportlock(space)
    lock.acquire_lock()
    try:
        return _absolute_import(space, modulename, baselevel,
                                fromlist_w, tentative)
    finally:
        lock.release_lock(silent_after_fork=True)

@jit.unroll_safe
def absolute_import_try(space, modulename, baselevel, fromlist_w):
    """ Only look up sys.modules, not actually try to load anything
    """
    w_path = None
    last_dot = 0
    rstring.check_utf8(modulename)
    if '.' not in modulename:
        w_mod = check_sys_modules_w(space, modulename)
        first = w_mod
        if fromlist_w is not None and w_mod is not None:
            w_path = try_getattr(space, w_mod, space.wrap(u'__path__'))
    else:
        level = 0
        first = None
        while last_dot >= 0:
            last_dot = modulename.find('.', last_dot + 1)
            if last_dot < 0:
                w_mod = check_sys_modules_w(space, modulename)
            else:
                w_mod = check_sys_modules_w(
                    space, rstring.assert_utf8(modulename[:last_dot]))
            if w_mod is None or space.is_w(w_mod, space.w_None):
                return None
            if level == baselevel:
                first = w_mod
            if fromlist_w is not None:
                w_path = try_getattr(space, w_mod, space.wrap(u'__path__'))
            level += 1
    if fromlist_w is not None:
        if w_path is not None:
            if len(fromlist_w) == 1 and space.eq_w(fromlist_w[0],
                                                   space.wrap(u'*')):
                w_all = try_getattr(space, w_mod, space.wrap(u'__all__'))
                if w_all is not None:
                    fromlist_w = space.fixedview(w_all)
            for w_name in fromlist_w:
                if try_getattr(space, w_mod, w_name) is None:
                    return None
        return w_mod
    return first

def _absolute_import(space, modulename, baselevel, fromlist_w, tentative):
    w = space.wrap

    if '/' in modulename or '\\' in modulename:
        raise OperationError(space.w_ImportError,
                             w(u"Import by filename is not supported."))

    w_mod = None
    rstring.check_utf8(modulename)
    parts = modulename.split('.')
    prefix = []
    w_path = None

    first = None
    level = 0

    for part in parts:
        rstring.check_utf8(part)
        w_mod = load_part(space, w_path, prefix, part, w_mod,
                          tentative=tentative)
        if w_mod is None:
            return None

        if baselevel == level:
            first = w_mod
            tentative = 0
        prefix.append(part)
        w_path = try_getattr(space, w_mod, w(u'__path__'))
        level += 1

    if fromlist_w is not None:
        if w_path is not None:
            if len(fromlist_w) == 1 and space.eq_w(fromlist_w[0], w(u'*')):
                w_all = try_getattr(space, w_mod, w(u'__all__'))
                if w_all is not None:
                    fromlist_w = space.fixedview(w_all)
            for w_name in fromlist_w:
                if try_getattr(space, w_mod, w_name) is None:
                    name, u_name = _convert_utf8(space, w_name)
                    if name is None or "\x00" in name:
                        raise OperationError(
                            space.w_ValueError,
                            space.wrap(u"Attribute name must be string"))
                    name = rstring.assert_str0(name)
                    load_part(space, w_path, prefix, name, w_mod, tentative=1)
        return w_mod
    else:
        return first

def find_in_meta_path(space, w_modulename, w_path):
    assert w_modulename is not None
    if w_path is None:
        w_path = space.w_None
    for w_hook in space.unpackiterable(space.sys.get("meta_path")):
        w_loader = space.call_method(w_hook, "find_module",
                                     w_modulename, w_path)
        if space.is_true(w_loader):
            return w_loader

def _getimporter(space, w_pathitem):
    # the function 'imp._getimporter' is a pypy-only extension
    w_path_importer_cache = space.sys.get("path_importer_cache")
    w_importer = space.finditem(w_path_importer_cache, w_pathitem)
    if w_importer is None:
        space.setitem(w_path_importer_cache, w_pathitem, space.w_None)
        for w_hook in space.unpackiterable(space.sys.get("path_hooks")):
            try:
                w_importer = space.call_function(w_hook, w_pathitem)
            except OperationError, e:
                if not e.match(space, space.w_ImportError):
                    raise
            else:
                break
        if w_importer is None:
            try:
                w_importer = space.call_function(
                    space.gettypefor(W_NullImporter), w_pathitem
                )
            except OperationError, e:
                if e.match(space, space.w_ImportError):
                    return None
                raise
        if space.is_true(w_importer):
            space.setitem(w_path_importer_cache, w_pathitem, w_importer)
    return w_importer

def find_in_path_hooks(space, w_modulename, w_pathitem):
    w_importer = _getimporter(space, w_pathitem)
    if w_importer is not None and space.is_true(w_importer):
        try:
            w_loader = space.call_method(w_importer, "find_module",
                                         w_modulename)
        except OperationError, e:
            if e.match(space, space.w_ImportError):
                return None
            raise
        if space.is_true(w_loader):
            return w_loader

class _WIN32Path(object):
    def __init__(self, path):
        self.path = path

    def as_unicode(self):
        return self.path

class W_NullImporter(W_Root):
    def __init__(self, space):
        pass

    def descr_init(self, space, w_path):
        self._descr_init(space, w_path, _WIN32)

    @specialize.arg(3)
    def _descr_init(self, space, w_path, win32):
        path = space.unicode0_w(w_path) if win32 else space.fsencode_w(w_path)
        if not path:
            raise OperationError(space.w_ImportError,
                                 space.wrap(u"empty pathname"))

        # Directory should not exist
        try:
            st = rposix.stat(_WIN32Path(path) if win32 else path)
        except OSError:
            pass
        else:
            if stat.S_ISDIR(st.st_mode):
                raise OperationError(space.w_ImportError,
                                     space.wrap(u"existing directory"))

    def find_module_w(self, space, __args__):
        return space.wrap(None)

W_NullImporter.typedef = TypeDef(
    'imp.NullImporter',
    __new__=generic_new_descr(W_NullImporter),
    __init__=interp2app(W_NullImporter.descr_init),
    find_module=interp2app(W_NullImporter.find_module_w),
    )

class FindInfo:
    def __init__(self, modtype, filename, stream,
                 suffix="", filemode="", w_loader=None):
        rstring.check_ascii(filemode)
        self.modtype = modtype
        self.filename = filename
        self.stream = stream
        self.suffix = suffix
        self.filemode = filemode
        self.w_loader = w_loader

    @staticmethod
    def fromLoader(w_loader):
        return FindInfo(IMP_HOOK, '', None, w_loader=w_loader)

def find_module(space, modulename, w_modulename, partname, w_path,
                use_loader=True):
    # Examin importhooks (PEP302) before doing the import
    if use_loader:
        w_loader  = find_in_meta_path(space, w_modulename, w_path)
        if w_loader:
            return FindInfo.fromLoader(w_loader)

    # XXX Check for frozen modules?
    #     when w_path is a string

    delayed_builtin = None
    w_lib_extensions = None

    if w_path is None:
        # check the builtin modules
        if modulename in space.builtin_modules:
            modulename = rstring.assert_ascii(modulename)
            delayed_builtin = FindInfo(C_BUILTIN, modulename, None)
            # a "real builtin module xx" shadows every file "xx.py" there
            # could possibly be; a "pseudo-extension module" does not, and
            # is only loaded at the point in sys.path where we find
            # '.../lib_pypy/__extensions__'.
            if modulename in space.MODULES_THAT_ALWAYS_SHADOW:
                return delayed_builtin
            w_lib_extensions = space.sys.get_state(space).w_lib_extensions
        w_path = space.sys.get('path')

    # XXX check frozen modules?
    #     when w_path is null

    if w_path is not None:
        for w_pathitem in space.unpackiterable(w_path):
            # sys.path_hooks import hook
            if (w_lib_extensions is not None and
                    space.eq_w(w_pathitem, w_lib_extensions)):
                return delayed_builtin
            if use_loader:
                w_loader = find_in_path_hooks(space, w_modulename, w_pathitem)
                if w_loader:
                    return FindInfo.fromLoader(w_loader)

            if not space.isinstance_w(w_pathitem, space.w_unicode):
                raise OperationError(space.w_ValueError,
                                     space.wrap(u"Path item must be string"))
            try:
                path = space.unicode_w(w_pathitem).encode('ascii')
            except UnicodeEncodeError:
                path = space.fsencode_w(w_pathitem)
            if '\x00' in path:
                raise OperationError(space.w_TypeError, space.wrap(
                    u'argument must be a unicode string '
                    'without NUL characters'))
            path = rstring.assert_str0(path)
            filepart = os.path.join(path, partname)
            if os.path.isdir(filepart) and case_ok(filepart):
                initfile = os.path.join(filepart, '__init__')
                modtype, _, _ = find_modtype(space, initfile)
                if modtype in (PY_SOURCE, PY_COMPILED):
                    return FindInfo(PKG_DIRECTORY, filepart, None)
                else:
                    msg = (u"Not importing directory '%s' missing __init__.py" %
                           fsdecode_w(space, filepart))
                    space.warn(space.wrap(msg), space.w_ImportWarning)
            modtype, suffix, filemode = find_modtype(space, filepart)
            try:
                if modtype in (PY_SOURCE, PY_COMPILED, C_EXTENSION):
                    assert suffix is not None
                    filename = filepart + suffix
                    stream = streamio.open_file_as_stream(filename, filemode)
                    try:
                        return FindInfo(modtype, filename, stream,
                                        suffix, filemode)
                    except:
                        stream.close()
                        raise
            except StreamErrors:
                pass   # XXX! must not eat all exceptions, e.g.
                       # Out of file descriptors.

    # not found
    return delayed_builtin

def _prepare_module(space, w_mod, filename, pkgdir):
    w = space.wrap
    space.sys.setmodule(w_mod)
    space.setattr(w_mod, w(u'__file__'), fsdecode(space, filename))
    space.setattr(w_mod, w(u'__doc__'), space.w_None)
    if pkgdir is not None:
        space.setattr(w_mod, w(u'__path__'),
                      space.newlist([fsdecode(space, pkgdir)]))

def add_module(space, w_name):
    w_mod = check_sys_modules(space, w_name)
    if w_mod is None:
        w_mod = space.wrap(Module(space, w_name))
        space.sys.setmodule(w_mod)
    return w_mod

def load_c_extension(space, filename, modulename):
    # the next line is mandatory to init cpyext
    space.getbuiltinmodule("cpyext")
    from pypy.module.cpyext.api import load_extension_module
    load_extension_module(space, filename, modulename)

def load_c_extension_w(space, filename, w_modulename):
    modulename, u_modulename = _convert_utf8(space, w_modulename)
    if modulename is None or '\x00' in modulename:
        raise OperationError(
            space.w_ValueError,
            space.wrap(u"Invalid module name %s" % u_modulename))
    load_c_extension(space, filename, modulename)

@jit.dont_look_inside
def load_module(space, w_modulename, find_info, reuse=False):
    if find_info is None:
        return

    if find_info.w_loader:
        return space.call_method(find_info.w_loader,
                                 "load_module", w_modulename)

    if find_info.modtype == C_BUILTIN:
        if find_info.filename not in space.builtin_modules:
            # This is closer (although still not identical) to CPython behavior
            # (CPython seems to ignore filename for c_builtin modules)
            # and returns None if nothing is found
            return
        return space.getbuiltinmodule(rstring.assert_ascii(find_info.filename),
                                      force_init=True, reuse=reuse)

    if find_info.modtype in (PY_SOURCE, PY_COMPILED, C_EXTENSION, PKG_DIRECTORY):
        w_mod = None
        if reuse:
            try:
                w_mod = space.getitem(space.sys.get('modules'), w_modulename)
            except OperationError, oe:
                if not oe.match(space, space.w_KeyError):
                    raise
        if w_mod is None:
            w_mod = space.wrap(Module(space, w_modulename))
        if find_info.modtype == PKG_DIRECTORY:
            pkgdir = find_info.filename
        else:
            pkgdir = None
        _prepare_module(space, w_mod, find_info.filename, pkgdir)

        try:
            if find_info.modtype == PY_SOURCE:
                load_source_module(
                    space, w_modulename, w_mod,
                    find_info.filename, find_info.stream.readall(),
                    find_info.stream.try_to_find_file_descriptor())
                return w_mod
            elif find_info.modtype == PY_COMPILED:
                magic = _r_long(find_info.stream)
                timestamp = _r_long(find_info.stream)
                load_compiled_module(space, w_modulename, w_mod,
                                     find_info.filename, magic, timestamp,
                                     find_info.stream.readall())
                return w_mod
            elif find_info.modtype == PKG_DIRECTORY:
                w_path = space.newlist([fsdecode(space, find_info.filename)])
                space.setattr(w_mod, space.wrap(u'__path__'), w_path)
                find_info = find_module(space, "__init__", None, "__init__",
                                        w_path, use_loader=False)
                if find_info is None:
                    return w_mod
                try:
                    load_module(space, w_modulename, find_info, reuse=True)
                finally:
                    find_info.stream.close()
                # fetch the module again, in case of "substitution"
                w_mod = check_sys_modules(space, w_modulename)
                return w_mod
            elif (find_info.modtype == C_EXTENSION and
                  space.config.objspace.usemodules.cpyext):
                load_c_extension_w(space, find_info.filename, w_modulename)
                return check_sys_modules(space, w_modulename)
        except OperationError:
            w_mods = space.sys.get('modules')
            space.call_method(w_mods, 'pop', w_modulename, space.w_None)
            raise

def load_part(space, w_path, prefix, partname, w_parent, tentative):
    w = space.wrap
    modulename = '.'.join(prefix + [partname])
    rstring.check_utf8(modulename)
    u_modulename = modulename.decode('utf-8')
    w_modulename = w(u_modulename)
    w_mod = check_sys_modules(space, w_modulename)

    if w_mod is not None:
        if not space.is_w(w_mod, space.w_None):
            return w_mod
    elif not prefix or w_path is not None:
        find_info = find_module(
            space, modulename, w_modulename, partname, w_path)

        try:
            if find_info:
                w_mod = load_module(space, w_modulename, find_info)
                try:
                    w_mod = space.getitem(space.sys.get("modules"),
                                          w_modulename)
                except OperationError, oe:
                    if not oe.match(space, space.w_KeyError):
                        raise
                    raise OperationError(space.w_ImportError, w_modulename)
                if w_parent is not None:
                    space.setattr(w_parent, w(partname), w_mod)
                return w_mod
        finally:
            if find_info:
                stream = find_info.stream
                if stream:
                    stream.close()

    if tentative:
        return None
    else:
        # ImportError
        raise OperationError(
            space.w_ImportError,
            space.wrap(u"No module named %s" % u_modulename))

@jit.dont_look_inside
def reload(space, w_module):
    """Reload the module.
    The module must have been successfully imported before."""
    if not space.is_w(space.type(w_module), space.type(space.sys)):
        raise OperationError(
            space.w_TypeError,
            space.wrap(u"reload() argument must be module"))

    w_modulename = space.getattr(w_module, space.wrap(u"__name__"))
    modulename, u_modulename = _convert_unicode_attr(space, w_modulename,
                                                     u'__name__')
    if not space.is_w(check_sys_modules(space, w_modulename), w_module):
        raise OperationError(space.w_ImportError,
                             space.wrap(u"reload(): module %s not in "
                                        "sys.modules" % u_modulename))

    try:
        w_mod = space.reloading_modules[modulename]
        # Due to a recursive reload, this module is already being reloaded.
        return w_mod
    except KeyError:
        pass

    space.reloading_modules[modulename] = w_module
    try:
        namepath = modulename.split('.')
        subname = namepath[-1]
        parent_name = '.'.join(namepath[:-1])
        if parent_name:
            rstring.check_utf8(parent_name)
            w_parent = check_sys_modules_w(space, parent_name)
            if w_parent is None:
                raise OperationError(space.w_ImportError,
                                     space.wrap(u"reload(): parent %s not in "
                                                "sys.modules" %
                                                parent_name.decode('utf-8')))
            w_path = space.getattr(w_parent, space.wrap(u"__path__"))
        else:
            w_path = None

        find_info = find_module(space, modulename, w_modulename,
                                subname, w_path)

        if not find_info:
            # ImportError
            raise OperationError(space.w_ImportError,
                                 space.wrap(u"No module named %s" %
                                            u_modulename))

        try:
            try:
                return load_module(space, w_modulename, find_info, reuse=True)
            finally:
                if find_info.stream:
                    find_info.stream.close()
        except:
            # load_module probably removed name from modules because of
            # the error.  Put back the original module object.
            space.sys.setmodule(w_module)
            raise
    finally:
        del space.reloading_modules[modulename]


# __________________________________________________________________
#
# import lock, to prevent two threads from running module-level code in
# parallel.  This behavior is more or less part of the language specs,
# as an attempt to avoid failure of 'from x import y' if module x is
# still being executed in another thread.

# This logic is tested in pypy.module.thread.test.test_import_lock.

class ImportRLock:

    def __init__(self, space):
        self.space = space
        self.lock = None
        self.lockowner = None
        self.lockcounter = 0

    def lock_held_by_someone_else(self):
        me = self.space.getexecutioncontext()   # used as thread ident
        return self.lockowner is not None and self.lockowner is not me

    def lock_held_by_anyone(self):
        return self.lockowner is not None

    def acquire_lock(self):
        # this function runs with the GIL acquired so there is no race
        # condition in the creation of the lock
        if self.lock is None:
            try:
                self.lock = self.space.allocate_lock()
            except CannotHaveLock:
                return
        me = self.space.getexecutioncontext()   # used as thread ident
        if self.lockowner is me:
            pass    # already acquired by the current thread
        else:
            self.lock.acquire(True)
            assert self.lockowner is None
            assert self.lockcounter == 0
            self.lockowner = me
        self.lockcounter += 1

    def release_lock(self, silent_after_fork):
        me = self.space.getexecutioncontext()   # used as thread ident
        if self.lockowner is not me:
            if self.lockowner is None and silent_after_fork:
                # Too bad.  This situation can occur if a fork() occurred
                # with the import lock held, and we're the child.
                return
            if self.lock is None:   # CannotHaveLock occurred
                return
            space = self.space
            raise OperationError(space.w_RuntimeError,
                                 space.wrap(u"not holding the import lock"))
        assert self.lockcounter > 0
        self.lockcounter -= 1
        if self.lockcounter == 0:
            self.lockowner = None
            self.lock.release()

    def reinit_lock(self):
        # Called after fork() to ensure that newly created child
        # processes do not share locks with the parent
        if self.lockcounter > 1:
            # Forked as a side effect of import
            self.lock = self.space.allocate_lock()
            me = self.space.getexecutioncontext()
            self.lock.acquire(True)
            # XXX: can the previous line fail?
            self.lockowner = me
            self.lockcounter -= 1
        else:
            self.lock = None
            self.lockowner = None
            self.lockcounter = 0

def getimportlock(space):
    return space.fromcache(ImportRLock)

# __________________________________________________________________
#
# .pyc file support

"""
   Magic word to reject .pyc files generated by other Python versions.
   It should change for each incompatible change to the bytecode.

   The value of CR and LF is incorporated so if you ever read or write
   a .pyc file in text mode the magic number will be wrong; also, the
   Apple MPW compiler swaps their values, botching string constants.

   CPython 2 uses values between 20121 - 62xxx
   CPython 3 uses values greater than 3000
   PyPy uses values under 3000

"""

# Depending on which opcodes are enabled, eg. CALL_METHOD we bump the version
# number by some constant
#
#     CPython + 0                  -- used by CPython without the -U option
#     CPython + 1                  -- used by CPython with the -U option
#     CPython + 7 = default_magic  -- used by PyPy (incompatible!)
#
from pypy.interpreter.pycode import default_magic
MARSHAL_VERSION_FOR_PYC = 2

def get_pyc_magic(space):
    # XXX CPython testing hack: delegate to the real imp.get_magic
    if not we_are_translated():
        if '__pypy__' not in space.builtin_modules:
            import struct
            magic = __import__('imp').get_magic()
            return struct.unpack('<i', magic)[0]

    return default_magic


def parse_source_module(space, pathname, source):
    """ Parse a source file and return the corresponding code object """
    ec = space.getexecutioncontext()
    pycode = ec.compiler.compile(source, pathname, 'exec', 0)
    return pycode

def exec_code_module(space, w_mod, code_w, pathname, cpathname,
                     write_paths=True):
    w_dict = space.getattr(w_mod, space.wrap(u'__dict__'))
    space.call_method(w_dict, 'setdefault',
                      space.wrap(u'__builtins__'),
                      space.wrap(space.builtin))
    if write_paths:
        if pathname is not None:
            w_pathname = get_sourcefile(space, pathname)
        else:
            w_pathname = fsdecode(code_w.co_filename)
        space.setitem(w_dict, space.wrap(u"__file__"), w_pathname)
        space.setitem(w_dict, space.wrap(u"__cached__"),
                      space.wrap(cpathname))
    code_w.exec_code(space, w_dict, w_dict)

def rightmost_sep(filename):
    "Like filename.rfind('/'), but also search for \\."
    index = filename.rfind(os.sep)
    if os.altsep is not None:
        index2 = filename.rfind(os.altsep)
        index = max(index, index2)
    return index

@signature(types.str0(), returns=types.str0())
def make_compiled_pathname(pathname):
    "Given the path to a .py file, return the path to its .pyc file."
    # foo.py -> __pycache__/foo.<tag>.pyc

    lastpos = rightmost_sep(pathname) + 1
    assert lastpos >= 0  # zero when slash, takes the full name
    fname = pathname[lastpos:]
    if lastpos > 0:
        # Windows: re-use the last separator character (/ or \\) when
        # appending the __pycache__ path.
        lastsep = pathname[lastpos-1]
    else:
        lastsep = os.sep
    ext = fname
    for i in range(len(fname)):
        if fname[i] == '.':
            ext = fname[:i + 1]

    result = (pathname[:lastpos] + "__pycache__" + lastsep +
              ext + PYC_TAG + '.pyc')
    return result

#@signature(types.str0(), returns=types.str0())
def make_source_pathname(pathname):
    "Given the path to a .pyc file, return the path to its .py file."
    # (...)/__pycache__/foo.<tag>.pyc -> (...)/foo.py

    right = rightmost_sep(pathname)
    if right < 0:
        return None
    left = rightmost_sep(pathname[:right]) + 1
    assert left >= 0
    if pathname[left:right] != '__pycache__':
        return None

    # Now verify that the path component to the right of the last
    # slash has two dots in it.
    rightpart = pathname[right + 1:]
    dot0 = rightpart.find('.') + 1
    if dot0 <= 0:
        return None
    dot1 = rightpart[dot0:].find('.') + 1
    if dot1 <= 0:
        return None
    # Too many dots?
    if rightpart[dot0 + dot1:].find('.') >= 0:
        return None

    result = pathname[:left] + rightpart[:dot0] + 'py'
    return result

def get_sourcefile(space, filename):
    start = len(filename) - 4
    stop = len(filename) - 1
    if not 0 <= start <= stop or filename[start:stop].lower() != ".py":
        return fsdecode(space, filename)
    py = make_source_pathname(filename)
    if py is None:
        py = filename[:-1]
    try:
        st = os.stat(py)
    except OSError:
        pass
    else:
        if stat.S_ISREG(st.st_mode):
            return fsdecode(space, py)
    return fsdecode(space, filename)

@jit.dont_look_inside
def load_source_module(space, w_modulename, w_mod, pathname, source, fd,
                       write_pyc=True):
    """
    Load a source module from a given file and return its module
    object.
    """
    w = space.wrap

    if space.config.objspace.usepycfiles:
        src_stat = os.fstat(fd)
        cpathname = make_compiled_pathname(pathname)
        mtime = int(src_stat[stat.ST_MTIME])
        mode = src_stat[stat.ST_MODE]
        stream = check_compiled_module(space, cpathname, mtime)
    else:
        cpathname = None
        mtime = 0
        mode = 0
        stream = None

    if stream:
        # existing and up-to-date .pyc file
        try:
            code_w = read_compiled_module(space, cpathname, stream.readall())
        finally:
            stream.close()
    else:
        code_w = parse_source_module(space, pathname, source)

        if space.config.objspace.usepycfiles and write_pyc:
            if not space.is_true(space.sys.get('dont_write_bytecode')):
                write_compiled_module(space, code_w, cpathname, mode, mtime)

    try:
        optimize = space.sys.get_flag('optimize')
    except RuntimeError:
        # during bootstrapping
        optimize = 0
    if optimize >= 2:
        code_w.remove_docstrings(space)

    update_code_filenames(space, code_w, pathname)
    exec_code_module(space, w_mod, code_w, pathname, cpathname)

    return w_mod

def update_code_filenames(space, code_w, pathname, oldname=None):
    assert isinstance(code_w, PyCode)
    if oldname is None:
        oldname = code_w.co_filename
    elif code_w.co_filename != oldname:
        return

    code_w.co_filename = pathname
    constants = code_w.co_consts_w
    for const in constants:
        if const is not None and isinstance(const, PyCode):
            update_code_filenames(space, const, pathname, oldname)

def _get_long(s):
    a = ord(s[0])
    b = ord(s[1])
    c = ord(s[2])
    d = ord(s[3])
    if d >= 0x80:
        d -= 0x100
    return a | (b<<8) | (c<<16) | (d<<24)

def _read_n(stream, n):
    buf = ''
    while len(buf) < n:
        data = stream.read(n - len(buf))
        if not data:
            raise streamio.StreamError("end of file")
        buf += data
    return buf

def _r_long(stream):
    s = _read_n(stream, 4)
    return _get_long(s)

def _w_long(stream, x):
    a = x & 0xff
    x >>= 8
    b = x & 0xff
    x >>= 8
    c = x & 0xff
    x >>= 8
    d = x & 0xff
    stream.write(chr(a) + chr(b) + chr(c) + chr(d))

def check_compiled_module(space, pycfilename, expected_mtime):
    """
    Check if a pyc file's magic number and mtime match.
    """
    stream = None
    try:
        stream = streamio.open_file_as_stream(pycfilename, "rb")
        magic = _r_long(stream)
        if magic != get_pyc_magic(space):
            stream.close()
            return None
        pyc_mtime = _r_long(stream)
        if pyc_mtime != expected_mtime:
            stream.close()
            return None
        return stream
    except StreamErrors:
        if stream:
            stream.close()
        return None    # XXX! must not eat all exceptions, e.g.
                       # Out of file descriptors.

def read_compiled_module(space, cpathname, strbuf):
    """ Read a code object from a file and check it for validity """

    w_marshal = space.getbuiltinmodule('marshal')
    w_code = space.call_method(w_marshal, 'loads', space.wrapbytes(strbuf))
    if not isinstance(w_code, Code):
        raise OperationError(space.w_ImportError,
                             space.wrap(u"Non-code object in %s" %
                                        fsdecode_w(space, cpathname)))
    return w_code

@jit.dont_look_inside
def load_compiled_module(space, w_modulename, w_mod, cpathname, magic,
                         timestamp, source, write_paths=True):
    """
    Load a module from a compiled file, execute it, and return its
    module object.
    """
    if magic != get_pyc_magic(space):
        raise OperationError(space.w_ImportError,
                             space.wrap(u"Bad magic number in %s" %
                                        fsdecode_w(space, cpathname)))
    #print "loading pyc file:", cpathname
    code_w = read_compiled_module(space, cpathname, source)
    try:
        optimize = space.sys.get_flag('optimize')
    except RuntimeError:
        # during bootstrapping
        optimize = 0
    if optimize >= 2:
        code_w.remove_docstrings(space)

    exec_code_module(space, w_mod, code_w, cpathname, cpathname, write_paths)

    return w_mod

def open_exclusive(space, cpathname, mode):
    try:
        os.unlink(cpathname)
    except OSError:
        pass

    flags = (os.O_EXCL|os.O_CREAT|os.O_WRONLY|os.O_TRUNC|
             streamio.O_BINARY)
    fd = os.open(cpathname, flags, mode)
    return streamio.fdopen_as_stream(fd, "wb")

def write_compiled_module(space, co, cpathname, src_mode, src_mtime):
    """
    Write a compiled module to a file, placing the time of last
    modification of its source into the header.
    Errors are ignored, if a write error occurs an attempt is made to
    remove the file.
    """
    # Ensure that the __pycache__ directory exists
    dirsep = rightmost_sep(cpathname)
    if dirsep < 0:
        return
    dirname = cpathname[:dirsep]
    mode = src_mode | 0333  # +wx
    try:
        os.mkdir(dirname, mode)
    except OSError:
        pass

    w_marshal = space.getbuiltinmodule('marshal')
    try:
        w_bytes = space.call_method(w_marshal, 'dumps', space.wrap(co),
                                    space.wrap(MARSHAL_VERSION_FOR_PYC))
        strbuf = space.bytes_w(w_bytes)
    except OperationError, e:
        if e.async(space):
            raise
        #print "Problem while marshalling %s, skipping" % cpathname
        return
    #
    # Careful here: we must not crash nor leave behind something that looks
    # too much like a valid pyc file but really isn't one.
    #
    mode = src_mode & ~0111
    try:
        stream = open_exclusive(space, cpathname, mode)
    except (OSError, StreamErrors):
        try:
            os.unlink(cpathname)
        except OSError:
            pass
        return

    try:
        try:
            # will patch the header later; write zeroes until we are sure that
            # the rest of the file is valid
            _w_long(stream, 0)   # pyc_magic
            _w_long(stream, 0)   # mtime
            stream.write(strbuf)

            # should be ok (XXX or should call os.fsync() to be sure?)
            stream.seek(0, 0)
            _w_long(stream, get_pyc_magic(space))
            _w_long(stream, src_mtime)
        finally:
            stream.close()
    except StreamErrors:
        try:
            os.unlink(cpathname)
        except OSError:
            pass
