""" This file makes open() and friends RPython. Note that RFile should not
be used directly and instead it's magically appearing each time you call
python builtin open()
"""

import os
from rpython.rlib import rposix
from rpython.rlib.rarithmetic import intmask
from rpython.rlib.rstring import StringBuilder
from rpython.rtyper.lltypesystem import rffi, lltype
from rpython.rtyper.tool import rffi_platform as platform
from rpython.translator.tool.cbuild import ExternalCompilationInfo

includes = ['stdio.h', 'sys/types.h']
if os.name == "posix":
    includes += ['unistd.h']
    ftruncate = 'ftruncate'
    fileno = 'fileno'
else:
    ftruncate = '_chsize'
    fileno = '_fileno'
eci = ExternalCompilationInfo(includes=includes)

def llexternal(*args, **kwargs):
    return rffi.llexternal(*args, compilation_info=eci, **kwargs)

class CConfig(object):
    _compilation_info_ = eci

    off_t = platform.SimpleType('off_t')

config = platform.configure(CConfig)

OFF_T = config['off_t']
FILEP = rffi.COpaquePtr("FILE")

c_open = llexternal('fopen', [rffi.CCHARP, rffi.CCHARP], FILEP)
c_close = llexternal('fclose', [FILEP], rffi.INT, releasegil=False)
c_fwrite = llexternal('fwrite', [rffi.CCHARP, rffi.SIZE_T, rffi.SIZE_T,
                                 FILEP], rffi.SIZE_T)
c_fread = llexternal('fread', [rffi.CCHARP, rffi.SIZE_T, rffi.SIZE_T,
                               FILEP], rffi.SIZE_T)
c_feof = llexternal('feof', [FILEP], rffi.INT)
c_ferror = llexternal('ferror', [FILEP], rffi.INT)
c_clearerror = llexternal('clearerr', [FILEP], lltype.Void)
c_fseek = llexternal('fseek', [FILEP, rffi.LONG, rffi.INT],
                     rffi.INT)
c_tmpfile = llexternal('tmpfile', [], FILEP)
c_fileno = llexternal(fileno, [FILEP], rffi.INT)
c_fdopen = llexternal(('_' if os.name == 'nt' else '') + 'fdopen',
                      [rffi.INT, rffi.CCHARP], FILEP)
c_ftell = llexternal('ftell', [FILEP], rffi.LONG)
c_fflush = llexternal('fflush', [FILEP], rffi.INT)
c_ftruncate = llexternal(ftruncate, [rffi.INT, OFF_T], rffi.INT, macro=True)

c_fgets = llexternal('fgets', [rffi.CCHARP, rffi.INT, FILEP],
                     rffi.CCHARP)

c_popen = llexternal('popen', [rffi.CCHARP, rffi.CCHARP], FILEP)
c_pclose = llexternal('pclose', [FILEP], rffi.INT, releasegil=False)

BASE_BUF_SIZE = 4096
BASE_LINE_SIZE = 100


def _error(ll_file):
    errno = c_ferror(ll_file)
    c_clearerror(ll_file)
    raise OSError(errno, os.strerror(errno))


def create_file(filename, mode="r", buffering=-1):
    assert buffering == -1
    assert filename is not None
    assert mode is not None
    ll_name = rffi.str2charp(filename)
    try:
        ll_mode = rffi.str2charp(mode)
        try:
            ll_f = c_open(ll_name, ll_mode)
            if not ll_f:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
        finally:
            lltype.free(ll_mode, flavor='raw')
    finally:
        lltype.free(ll_name, flavor='raw')
    return RFile(ll_f)


def create_temp_rfile():
    res = c_tmpfile()
    if not res:
        errno = rposix.get_errno()
        raise OSError(errno, os.strerror(errno))
    return RFile(res)

def create_fdopen_rfile(fd, mode="r"):
    assert mode is not None
    ll_mode = rffi.str2charp(mode)
    try:
        ll_f = c_fdopen(rffi.cast(rffi.INT, fd), ll_mode)
        if not ll_f:
            errno = rposix.get_errno()
            raise OSError(errno, os.strerror(errno))
    finally:
        lltype.free(ll_mode, flavor='raw')
    return RFile(ll_f)

def create_popen_file(command, type):
    ll_command = rffi.str2charp(command)
    try:
        ll_type = rffi.str2charp(type)
        try:
            ll_f = c_popen(ll_command, ll_type)
            if not ll_f:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
        finally:
            lltype.free(ll_type, flavor='raw')
    finally:
        lltype.free(ll_command, flavor='raw')
    return RPopenFile(ll_f)


class RFile(object):
    def __init__(self, ll_file):
        self.ll_file = ll_file

    def write(self, value):
        assert value is not None
        ll_file = self.ll_file
        if not ll_file:
            raise ValueError("I/O operation on closed file")
        assert value is not None
        ll_value = rffi.get_nonmovingbuffer(value)
        try:
            # note that since we got a nonmoving buffer, it is either raw
            # or already cannot move, so the arithmetics below are fine
            length = len(value)
            bytes = c_fwrite(ll_value, 1, length, ll_file)
            if bytes != length:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
        finally:
            rffi.free_nonmovingbuffer(value, ll_value)

    def close(self):
        """Closes the described file.

        Attention! Unlike Python semantics, `close' does not return `None' upon
        success but `0', to be able to return an exit code for popen'ed files.

        The actual return value may be determined with os.WEXITSTATUS.
        """
        res = 0
        ll_f = self.ll_file
        if ll_f:
            # double close is allowed
            self.ll_file = lltype.nullptr(FILEP.TO)
            res = self._do_close(ll_f)
            if res == -1:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
        return res

    _do_close = staticmethod(c_close)    # overridden in RPopenFile

    def read(self, size=-1):
        # XXX CPython uses a more delicate logic here
        ll_file = self.ll_file
        if not ll_file:
            raise ValueError("I/O operation on closed file")
        if size < 0:
            # read the entire contents
            buf = lltype.malloc(rffi.CCHARP.TO, BASE_BUF_SIZE, flavor='raw')
            try:
                s = StringBuilder()
                while True:
                    returned_size = c_fread(buf, 1, BASE_BUF_SIZE, ll_file)
                    returned_size = intmask(returned_size)  # is between 0 and BASE_BUF_SIZE
                    if returned_size == 0:
                        if c_feof(ll_file):
                            # ok, finished
                            return s.build()
                        raise _error(ll_file)
                    s.append_charpsize(buf, returned_size)
            finally:
                lltype.free(buf, flavor='raw')
        else:
            with rffi.scoped_alloc_buffer(size) as buf:
                returned_size = c_fread(buf.raw, 1, size, ll_file)
                returned_size = intmask(returned_size)  # is between 0 and size
                if returned_size == 0:
                    if not c_feof(ll_file):
                        raise _error(ll_file)
                s = buf.str(returned_size)
            return s

    def seek(self, pos, whence=0):
        ll_file = self.ll_file
        if not ll_file:
            raise ValueError("I/O operation on closed file")
        res = c_fseek(ll_file, pos, whence)
        if res == -1:
            errno = rposix.get_errno()
            raise OSError(errno, os.strerror(errno))

    def fileno(self):
        if self.ll_file:
            return intmask(c_fileno(self.ll_file))
        raise ValueError("I/O operation on closed file")

    def tell(self):
        if self.ll_file:
            res = intmask(c_ftell(self.ll_file))
            if res == -1:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
            return res
        raise ValueError("I/O operation on closed file")

    def flush(self):
        if self.ll_file:
            res = c_fflush(self.ll_file)
            if res != 0:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
            return
        raise ValueError("I/O operation on closed file")

    def truncate(self, arg=-1):
        if self.ll_file:
            if arg == -1:
                arg = self.tell()
            res = c_ftruncate(self.fileno(), arg)
            if res == -1:
                errno = rposix.get_errno()
                raise OSError(errno, os.strerror(errno))
            return
        raise ValueError("I/O operation on closed file")

    def __del__(self):
        self.close()

    def _readline1(self, raw_buf):
        result = c_fgets(raw_buf, BASE_LINE_SIZE, self.ll_file)
        if not result:
            if c_feof(self.ll_file):   # ok
                return 0
            raise _error(self.ll_file)
        #
        # Assume that fgets() works as documented, and additionally
        # never writes beyond the final \0, which the CPython
        # fileobject.c says appears to be the case everywhere.
        # The only case where the buffer was not big enough is the
        # case where the buffer is full, ends with \0, and doesn't
        # end with \n\0.
        strlen = 0
        while raw_buf[strlen] != '\0':
            strlen += 1
        if (strlen == BASE_LINE_SIZE - 1 and
                raw_buf[BASE_LINE_SIZE - 2] != '\n'):
            return -1    # overflow!
        # common case
        return strlen

    def readline(self):
        if self.ll_file:
            with rffi.scoped_alloc_buffer(BASE_LINE_SIZE) as buf:
                c = self._readline1(buf.raw)
                if c >= 0:
                    return buf.str(c)
                #
                # this is the rare case: the line is longer than BASE_LINE_SIZE
                s = StringBuilder()
                while True:
                    s.append_charpsize(buf.raw, BASE_LINE_SIZE - 1)
                    c = self._readline1(buf.raw)
                    if c >= 0:
                        break
                #
                s.append_charpsize(buf.raw, c)
                return s.build()
        raise ValueError("I/O operation on closed file")


class RPopenFile(RFile):
    _do_close = staticmethod(c_pclose)
