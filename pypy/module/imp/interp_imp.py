from pypy.module.imp import importing
from rpython.rlib import streamio, rstring
from rpython.rlib.streamio import StreamErrors
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.module import Module
from pypy.interpreter.gateway import unwrap_spec
from pypy.interpreter.pyparser import pyparse
from pypy.objspace.std import unicodeobject
from pypy.module._io.interp_iobase import W_IOBase
from pypy.module._io import interp_io
from pypy.interpreter.streamutil import wrap_streamerror


def get_suffixes(space):
    w = space.wrap
    suffixes_w = []
    if space.config.objspace.usemodules.cpyext:
        suffixes_w.append(
            space.newtuple([w(importing.get_so_extension(space)),
                            w('rb'), w(importing.C_EXTENSION)]))
    suffixes_w.extend([
        space.newtuple([w('.py'), w('U'), w(importing.PY_SOURCE)]),
        space.newtuple([w('.pyc'), w('rb'), w(importing.PY_COMPILED)]),
        ])
    return space.newlist(suffixes_w)

def get_magic(space):
    x = importing.get_pyc_magic(space)
    a = x & 0xff
    x >>= 8
    b = x & 0xff
    x >>= 8
    c = x & 0xff
    x >>= 8
    d = x & 0xff
    return space.wrapbytes(chr(a) + chr(b) + chr(c) + chr(d))

def get_tag(space):
    """get_tag() -> string
    Return the magic tag for .pyc or .pyo files."""
    return space.wrap(importing.PYC_TAG)

def get_file(space, w_file, filename, filemode):
    if space.is_none(w_file):
        try:
            return streamio.open_file_as_stream(filename, filemode)
        except streamio.StreamErrors, e:
            # XXX this is not quite the correct place, but it will do for now.
            # XXX see the issue which I'm sure exists already but whose number
            # XXX I cannot find any more...
            raise wrap_streamerror(space, e)
    else:
        w_iobase = space.interp_w(W_IOBase, w_file)
        # XXX: not all W_IOBase have a fileno method: in that case, we should
        # probably raise a TypeError?
        fd = space.int_w(space.call_method(w_iobase, 'fileno'))
        return streamio.fdopen_as_stream(fd, filemode)

def find_module(space, w_name, w_path=None):
    if not space.isinstance_w(w_name, space.unicode_w):
        raise oefmt(space.w_TypeError, "name must be a str, not %T", w_name)
    name = space.fsencode_w(w_name)
    if space.is_none(w_path):
        w_path = None

    find_info = importing.find_module(
        space, name, w_name, name, w_path, use_loader=False)
    if not find_info:
        raise OperationError(space.w_ImportError,
                             space.wrap(u"No module named %s" %
                                        space.unicode_w(w_name)))

    w_filename = importing.fsdecode(space, find_info.filename)
    stream = find_info.stream

    if stream is not None:
        encoding = None
        if find_info.modtype == importing.PY_SOURCE:
            # try to find the declared encoding
            top = stream.readline()
            top += stream.readline()
            stream.seek(0, 0) # reset position
            stream.flush()
            encoding = pyparse._check_for_encoding(top)
            if encoding is None:
                encoding = unicodeobject.getdefaultencoding(space)
        #
        # in python2, both CPython and PyPy pass the filename to
        # open(). However, CPython 3 just passes the fd, so the returned file
        # object doesn't have a name attached. We do the same in PyPy, because
        # there is no easy way to attach the filename -- too bad
        fd = stream.try_to_find_file_descriptor()
        try:
            w_fileobj = interp_io.open(space, space.wrap(fd),
                                       find_info.filemode, encoding=encoding)
        except OperationError as e:
            if e.match(space, space.w_LookupError):
                raise OperationError(space.w_SyntaxError,
                                     space.str(e.get_w_value(space)))
            raise
    else:
        w_fileobj = space.w_None
    w_import_info = space.newtuple(
        [space.wrap(find_info.suffix),
         space.wrap(find_info.filemode),
         space.wrap(find_info.modtype)])
    return space.newtuple([w_fileobj, w_filename, w_import_info])

def load_module(space, w_name, w_file, w_filename, w_info):
    w_suffix, w_filemode, w_modtype = space.unpackiterable(w_info, 3)

    filename = space.fsencode_w(w_filename)
    if w_filemode is not space.w_None:
        filemode = None
    elif not space.isinstance_w(w_filemode, space.w_unicode):
        raise oefmt(space.w_ValueError, "filemode must be str not %T",
                    w_filemode)
    else:
        u_filemode = space.unicode_w(w_filemode)
        try:
            filemode = u_filemode.encode('ascii')
        except UnicodeEncodeError:
            raise OperationError(space.w_ValueError,
                                 space.wrap(u"Invalid filemode %s" %
                                            u_filemode))
    if space.is_w(w_file, space.w_None):
        stream = None
    else:
        stream = get_file(space, w_file, filename, filemode)

    find_info = importing.FindInfo(space.int_w(w_modtype), filename, stream,
                                   space.fsencode_w(w_suffix), filemode)
    return importing.load_module(space, w_name, find_info, reuse=True)

def load_source(space, w_modulename, w_filename, w_file=None):
    if not space.isinstance_w(w_modulename, space.unicode_w):
        raise oefmt(space.w_TypeError, "modulename must be a str, not %T",
                    w_modulename)
    if not space.isinstance_w(w_filename, space.unicode_w):
        raise oefmt(space.w_TypeError, "filename must be a str, not %T",
                    w_filename)
    filename = space.fsencode_w(w_filename)

    stream = get_file(space, w_file, filename, 'U')

    w_mod = space.wrap(Module(space, w_modulename))
    importing._prepare_module(space, w_mod, filename, None)

    importing.load_source_module(
        space, w_modulename, w_mod,
        filename, stream.readall(), stream.try_to_find_file_descriptor())
    if space.is_none(w_file):
        stream.close()
    return w_mod

@unwrap_spec(filename='fsencode', write_paths=bool)
def _run_compiled_module(space, w_modulename, filename, w_file, w_module,
                         write_paths=True):
    # the function 'imp._run_compiled_module' is a pypy-only extension
    stream = get_file(space, w_file, filename, 'rb')

    magic = importing._r_long(stream)
    timestamp = importing._r_long(stream)

    importing.load_compiled_module(
        space, w_modulename, w_module, filename, magic, timestamp,
        stream.readall(), write_paths)
    if space.is_none(w_file):
        stream.close()

@unwrap_spec(filename='fsencode')
def load_compiled(space, w_modulename, filename, w_file=None):
    w_mod = space.wrap(Module(space, w_modulename))
    importing._prepare_module(space, w_mod, filename, None)
    _run_compiled_module(space, w_modulename, filename, w_file, w_mod)
    return w_mod

@unwrap_spec(filename='fsencode')
def load_dynamic(space, w_modulename, filename, w_file=None):
    if not space.config.objspace.usemodules.cpyext:
        raise OperationError(space.w_ImportError,
                             space.wrap(u"Not implemented"))
    importing.load_c_extension_w(space, filename, w_modulename)
    return importing.check_sys_modules(space, w_modulename)

def new_module(space, w_name):
    return space.wrap(Module(space, w_name, add_package=False))

def init_builtin(space, w_name):
    name = space.str0_w(w_name)
    if name not in space.builtin_modules:
        return
    if space.finditem(space.sys.get('modules'), w_name) is not None:
        raise OperationError(
            space.w_ImportError,
            space.wrap(u"cannot initialize a built-in module twice in PyPy"))
    return space.getbuiltinmodule(rstring.assert_ascii(name))

def init_frozen(space, w_name):
    return None

def is_builtin(space, w_name):
    name = space.str0_w(w_name)
    if name not in space.builtin_modules:
        return space.wrap(0)
    if space.finditem(space.sys.get('modules'), w_name) is not None:
        return space.wrap(-1)   # cannot be initialized again
    return space.wrap(1)

def is_frozen(space, w_name):
    return space.w_False

#__________________________________________________________________

def lock_held(space):
    if space.config.objspace.usemodules.thread:
        return space.wrap(importing.getimportlock(space).lock_held_by_anyone())
    else:
        return space.w_False

def acquire_lock(space):
    if space.config.objspace.usemodules.thread:
        importing.getimportlock(space).acquire_lock()

def release_lock(space):
    if space.config.objspace.usemodules.thread:
        importing.getimportlock(space).release_lock(silent_after_fork=False)

def reinit_lock(space):
    if space.config.objspace.usemodules.thread:
        importing.getimportlock(space).reinit_lock()

@unwrap_spec(pathname='fsencode')
def cache_from_source(space, pathname, w_debug_override=None):
    """cache_from_source(path, [debug_override]) -> path
    Given the path to a .py file, return the path to its .pyc/.pyo file.

    The .py file does not need to exist; this simply returns the path to the
    .pyc/.pyo file calculated as if the .py file were imported.  The extension
    will be .pyc unless __debug__ is not defined, then it will be .pyo.

    If debug_override is not None, then it must be a boolean and is taken as
    the value of __debug__ instead."""
    return space.fsdecode(space.wrapbytes(
            importing.make_compiled_pathname(pathname)))

@unwrap_spec(pathname='fsencode')
def source_from_cache(space, pathname):
    """source_from_cache(path) -> path
    Given the path to a .pyc./.pyo file, return the path to its .py file.

    The .pyc/.pyo file does not need to exist; this simply returns the path to
    the .py file calculated to correspond to the .pyc/.pyo file.  If path
    does not conform to PEP 3147 format, ValueError will be raised."""
    sourcename = importing.make_source_pathname(pathname)
    if sourcename is None:
        raise oefmt(space.w_ValueError,
                    "Not a PEP 3147 pyc path: %s", pathname)
    return space.fsdecode(space.wrapbytes(sourcename))
