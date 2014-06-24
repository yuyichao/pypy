#!/usr/bin/env python
""" packages PyPy, provided that it's already built.
It uses 'pypy/goal/pypy-c' and parts of the rest of the working
copy.  Usage:

    package.py [--options]

Usually you would do:   package.py --version-name pypy-VER-PLATFORM
The output is found in the directory from --builddir,
by default /tmp/usession-YOURNAME/build/.
"""

import shutil
import sys
import os
#Add toplevel repository dir to sys.path
basedir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0,basedir)
import py
import fnmatch
import subprocess
import glob

if sys.version_info < (2,6): py.test.skip("requires 2.6 so far")

USE_ZIPFILE_MODULE = sys.platform == 'win32'

STDLIB_VER = "2.7"

def ignore_patterns(*patterns):
    """Function that can be used as copytree() ignore parameter.

    Patterns is a sequence of glob-style patterns
    that are used to exclude files"""
    def _ignore_patterns(path, names):
        ignored_names = []
        for pattern in patterns:
            ignored_names.extend(fnmatch.filter(names, pattern))
        return set(ignored_names)
    return _ignore_patterns

class PyPyCNotFound(Exception):
    pass

class MissingDependenciesError(Exception):
    pass

def fix_permissions(dirname):
    if sys.platform != 'win32':
        os.system("chmod -R a+rX %s" % dirname)
        os.system("chmod -R g-w %s" % dirname)

sep_template = "\nThis copy of PyPy includes a copy of %s, which is licensed under the following terms:\n\n"

def generate_license_linux(basedir, options):
    base_file = str(basedir.join('LICENSE'))
    with open(base_file) as fid:
        txt = fid.read()
    searches = [("bzip2","libbz2-*", "copyright", '---------'),
                ("openssl", "openssl*", "copyright", 'LICENSE ISSUES'),
               ]
    if not options.no_tk:
        name = 'Tcl/Tk'
        txt += "License for '%s'" %name
        txt += '\n' + "="*(14 + len(name)) + '\n'
        txt += sep_template % name
        base_file = str(basedir.join('lib_pypy/_tkinter/license.terms'))
        with open(base_file, 'r') as fid:
            txt += fid.read()
    for name, pat, fname, first_line in searches:
        txt += "License for '" + name + "'"
        txt += '\n' + "="*(14 + len(name)) + '\n'
        txt += sep_template % name
        dirs = glob.glob(options.license_base + "/" +pat)
        if not dirs:
            raise ValueError, "Could not find "+ options.license_base + "/" + pat
        if len(dirs) > 2:
            raise ValueError, "Multiple copies of "+pat
        dir = dirs[0]
        with open(os.path.join(dir, fname)) as fid:
            # Read up to the line dividing the packaging header from the actual copyright
            for line in fid:
                if first_line in line:
                    break
            txt += line
            for line in fid:
                txt += line
            if len(line.strip())<1:
                txt += '\n'
    txt += third_party_header
    # Do something for gdbm, which is GPL
    txt += gdbm_bit
    return txt

def generate_license_windows(basedir, options):
    base_file = str(basedir.join('LICENSE'))
    with open(base_file) as fid:
        txt = fid.read()
    # shutil.copyfileobj(open("crtlicense.txt"), out) # We do not ship msvc runtime files
    if not options.no_tk:
        name = 'Tcl/Tk'
        txt += "License for '%s'" %name
        txt += '\n' + "="*(14 + len(name)) + '\n'
        txt += sep_template % name
        base_file = str(basedir.join('lib_pypy/_tkinter/license.terms'))
        with open(base_file, 'r') as fid:
            txt += fid.read()
    for name, pat, file in (("bzip2","bzip2-*", "LICENSE"),
                      ("openssl", "openssl-*", "LICENSE")):
        txt += sep_template % name
        dirs = glob.glob(options.license_base + "/" +pat)
        if not dirs:
            raise ValueError, "Could not find "+ options.license_base + "/" + pat
        if len(dirs) > 2:
            raise ValueError, "Multiple copies of "+pat
        dir = dirs[0]
        with open(os.path.join(dir, file)) as fid:
            txt += fid.read()
    return txt

def generate_license_darwin(basedir, options):
    # where are copyright files on macos?
    return generate_license_linux(basedir, options)

if sys.platform == 'win32':
    generate_license = generate_license_windows
elif sys.platform == 'darwin':
    generate_license = generate_license_darwin
else:
    generate_license = generate_license_linux

def create_cffi_import_libraries(pypy_c, options):
    modules = ['_sqlite3']
    subprocess.check_call([str(pypy_c), '-c', 'import _sqlite3'])
    if not sys.platform == 'win32':
        modules += ['_curses', 'syslog', 'gdbm', '_sqlite3']
    if not options.no_tk:
        modules.append(('_tkinter'))
    for module in modules:
        try:
            subprocess.check_call([str(pypy_c), '-c', 'import ' + module])
        except subprocess.CalledProcessError:
            print >>sys.stderr, """Building {0} bindings failed.
You can either install development headers package or
add --without-{0} option to skip packaging binary CFFI extension.""".format(module)
            raise MissingDependenciesError(module)

def create_package(basedir, options):
    retval = 0
    name = options.name
    if not name:
        name = 'pypy-nightly'
    rename_pypy_c = options.pypy_c
    override_pypy_c = options.override_pypy_c

    basedir = py.path.local(basedir)
    if not override_pypy_c:
        basename = 'pypy-c'
        if sys.platform == 'win32':
            basename += '.exe'
        pypy_c = basedir.join('pypy', 'goal', basename)
    else:
        pypy_c = py.path.local(override_pypy_c)
    if not pypy_c.check():
        print pypy_c
        if os.path.isdir(os.path.dirname(str(pypy_c))):
            raise PyPyCNotFound(
                'Please compile pypy first, using translate.py,'
                ' or check that you gave the correct path'
                ' (see docstring for more info)')
        else:
            raise PyPyCNotFound(
                'Bogus path: %r does not exist (see docstring for more info)'
                % (os.path.dirname(str(pypy_c)),))
    if not options.no_cffi:
        try:
            create_cffi_import_libraries(pypy_c, options)
        except MissingDependenciesError:
            # This is a non-fatal error
            retval = -1

    if sys.platform == 'win32' and not rename_pypy_c.lower().endswith('.exe'):
        rename_pypy_c += '.exe'
    binaries = [(pypy_c, rename_pypy_c)]
    #
    builddir = options.builddir
    pypydir = builddir.ensure(name, dir=True)
    includedir = basedir.join('include')
    # Recursively copy all headers, shutil has only ignore
    # so we do a double-negative to include what we want
    def copyonly(dirpath, contents):
        return set(contents) - set(
            shutil.ignore_patterns('*.h', '*.incl')(dirpath, contents),
        )
    shutil.copytree(str(includedir), str(pypydir.join('include')))
    pypydir.ensure('include', dir=True)

    if sys.platform == 'win32':
        # Can't rename a DLL: it is always called 'libpypy-c.dll'
        win_extras = ['libpypy-c.dll', 'libexpat.dll', 'sqlite3.dll',
                          'libeay32.dll', 'ssleay32.dll']
        if not options.no_tk:
            win_extras += ['tcl85.dll', 'tk85.dll']

        for extra in win_extras:
            p = pypy_c.dirpath().join(extra)
            if not p.check():
                p = py.path.local.sysfind(extra)
                if not p:
                    print "%s not found, expect trouble if this is a shared build" % (extra,)
                    continue
            print "Picking %s" % p
            binaries.append((p, p.basename))
        importlib_name = 'python27.lib'
        if pypy_c.dirpath().join(importlib_name).check():
            shutil.copyfile(str(pypy_c.dirpath().join(importlib_name)),
                        str(pypydir.join('include/python27.lib')))
            print "Picking %s as %s" % (pypy_c.dirpath().join(importlib_name),
                        pypydir.join('include/python27.lib'))
        else:
            pass
            # XXX users will complain that they cannot compile cpyext
            # modules for windows, has the lib moved or are there no
            # exported functions in the dll so no import library is created?
        if not options.no_tk:
            try:
                p = pypy_c.dirpath().join('tcl85.dll')
                if not p.check():
                    p = py.path.local.sysfind('tcl85.dll')
                tktcldir = p.dirpath().join('..').join('lib')
                shutil.copytree(str(tktcldir), str(pypydir.join('tcl')))
            except WindowsError:
                print >>sys.stderr, """Packaging Tk runtime failed.
tk85.dll and tcl85.dll found, expecting to find runtime in ..\\lib
directory next to the dlls, as per build instructions."""
                import traceback;traceback.print_exc()
                raise MissingDependenciesError('Tk runtime')

    # Careful: to copy lib_pypy, copying just the hg-tracked files
    # would not be enough: there are also ctypes_config_cache/_*_cache.py.
    shutil.copytree(str(basedir.join('lib-python').join(STDLIB_VER)),
                    str(pypydir.join('lib-python').join(STDLIB_VER)),
                    ignore=ignore_patterns('.svn', 'py', '*.pyc', '*~'))
    shutil.copytree(str(basedir.join('lib_pypy')),
                    str(pypydir.join('lib_pypy')),
                    ignore=ignore_patterns('.svn', 'py', '*.pyc', '*~',
                                           '*.c', '*.o'))
    for file in ['README.rst',]:
        shutil.copy(str(basedir.join(file)), str(pypydir))
    for file in ['_testcapimodule.c', '_ctypes_test.c']:
        shutil.copyfile(str(basedir.join('lib_pypy', file)),
                        str(pypydir.join('lib_pypy', file)))
    try:
        license = generate_license(basedir, options)
        with open(str(pypydir.join('LICENSE')), 'w') as LICENSE:
            LICENSE.write(license)
    except:
        # Non-fatal error, use original LICENCE file
        import traceback;traceback.print_exc()
        base_file = str(basedir.join('LICENSE'))
        with open(base_file) as fid:
            license = fid.read()
        with open(str(pypydir.join('LICENSE')), 'w') as LICENSE:
            LICENSE.write(license)
        retval = -1
    #
    spdir = pypydir.ensure('site-packages', dir=True)
    shutil.copy(str(basedir.join('site-packages', 'README')), str(spdir))
    #
    if sys.platform == 'win32':
        bindir = pypydir
    else:
        bindir = pypydir.join('bin')
        bindir.ensure(dir=True)
    for source, target in binaries:
        archive = bindir.join(target)
        shutil.copy(str(source), str(archive))
    fix_permissions(builddir)

    old_dir = os.getcwd()
    try:
        os.chdir(str(builddir))
        if not options.nostrip:
            for source, target in binaries:
                if sys.platform == 'win32':
                    pass
                elif sys.platform == 'darwin':
                    # 'strip' fun: see issue #587 for why -x
                    os.system("strip -x " + str(bindir.join(target)))    # ignore errors
                else:
                    os.system("strip " + str(bindir.join(target)))    # ignore errors
        #
        if USE_ZIPFILE_MODULE:
            import zipfile
            archive = str(builddir.join(name + '.zip'))
            zf = zipfile.ZipFile(archive, 'w',
                                 compression=zipfile.ZIP_DEFLATED)
            for (dirpath, dirnames, filenames) in os.walk(name):
                for fnname in filenames:
                    filename = os.path.join(dirpath, fnname)
                    zf.write(filename)
            zf.close()
        else:
            archive = str(builddir.join(name + '.tar.bz2'))
            if sys.platform == 'darwin' or sys.platform.startswith('freebsd'):
                print >>sys.stderr, """Warning: tar on current platform does not suport overriding the uid and gid
for its contents. The tarball will contain your uid and gid. If you are
building the actual release for the PyPy website, you may want to be
using another platform..."""
                e = os.system('tar --numeric-owner -cvjf ' + archive + " " + name)
            elif sys.platform == 'cygwin':
                e = os.system('tar --owner=Administrator --group=Administrators --numeric-owner -cvjf ' + archive + " " + name)
            else:
                e = os.system('tar --owner=root --group=root --numeric-owner -cvjf ' + archive + " " + name)
            if e:
                raise OSError('"tar" returned exit status %r' % e)
    finally:
        os.chdir(old_dir)
    if options.targetdir:
        print "Copying %s to %s" % (archive, options.targetdir)
        shutil.copy(archive, options.targetdir)
    else:
        print "Ready in %s" % (builddir,)
    return retval, builddir # for tests

def package(*args):
    try:
        import argparse
    except ImportError:
        import imp
        argparse = imp.load_source('argparse', 'lib-python/2.7/argparse.py')
    if sys.platform == 'win32':
        pypy_exe = 'pypy.exe'
        license_base = os.path.join(basedir, r'..\..\..\local') # as on buildbot YMMV
    else:
        pypy_exe = 'pypy'
        license_base = '/usr/share/doc'
    parser = argparse.ArgumentParser()
    args = list(args)
    args[0] = str(args[0])
    parser.add_argument('--without-tk', dest='no_tk', action='store_true',
        help='build and package the cffi tkinter module')
    parser.add_argument('--without-cffi', dest='no_cffi', action='store_true',
        help='do not pre-import any cffi modules')
    parser.add_argument('--nostrip', dest='nostrip', action='store_true',
        help='do not strip the exe, making it ~10MB larger')
    parser.add_argument('--rename_pypy_c', dest='pypy_c', type=str, default=pypy_exe,
        help='target executable name, defaults to "pypy"')
    parser.add_argument('--archive-name', dest='name', type=str, default='',
        help='pypy-VER-PLATFORM')
    parser.add_argument('--license_base', type=str, default=license_base,
        help='where to start looking for third party upstream licensing info')
    parser.add_argument('--builddir', type=str, default='',
        help='tmp dir for packaging')
    parser.add_argument('--targetdir', type=str, default='',
        help='destination dir for archive')
    parser.add_argument('--override_pypy_c', type=str, default='',
        help='use as pypy exe instead of pypy/goal/pypy-c')
    # Positional arguments, for backward compatability with buldbots
    parser.add_argument('extra_args', help='optional interface to positional arguments', nargs=argparse.REMAINDER,
        metavar='[root-pypy-dir] [name-of-archive] [name-of-pypy-c] [destination-for-tarball] [pypy-c-path]',
        )
    options = parser.parse_args(args)

    # Handle positional arguments, choke if both methods are used
    for i,target, default in ([1, 'name', ''], [2, 'pypy_c', pypy_exe],
                              [3, 'targetdir', ''], [4,'override_pypy_c', '']):
        if len(options.extra_args)>i:
            if getattr(options, target) != default:
                print 'positional argument',i,target,'already has value',getattr(options, target)
                parser.print_help()
                return
            setattr(options, target, options.extra_args[i])
    if os.environ.has_key("PYPY_PACKAGE_NOSTRIP"):
        options.nostrip = True

    if os.environ.has_key("PYPY_PACKAGE_WITHOUTTK"):
        options.tk = True
    if not options.builddir:
        # The import actually creates the udir directory
        from rpython.tool.udir import udir
        options.builddir = udir.ensure("build", dir=True)
    assert '/' not in options.pypy_c
    return create_package(basedir, options)


third_party_header = '''\n\nLicenses and Acknowledgements for Incorporated Software
=======================================================

This section is an incomplete, but growing list of licenses and acknowledgements
for third-party software incorporated in the PyPy distribution.

'''

gdbm_bit = '''gdbm
----

The gdbm module includes code from gdbm.h, which is distributed under the terms
of the GPL license version 2 or any later version.
'''


if __name__ == '__main__':
    import sys
    retval, _ = package(*sys.argv[1:])
    sys.exit(retval)
