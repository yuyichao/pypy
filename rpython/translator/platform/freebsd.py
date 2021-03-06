"""Support for FreeBSD."""

import os
from rpython.translator.platform.bsd import BSD

class Freebsd(BSD):
    name = "freebsd"

    link_flags = ['-pthread'] + os.environ.get('LDFLAGS', '').split()
    cflags = ['-O3', '-pthread', '-fomit-frame-pointer'
             ] + os.environ.get('CFLAGS', '').split()

class Freebsd_64(Freebsd):
    shared_only = ('-fPIC',)

class GNUkFreebsd(Freebsd):
    DEFAULT_CC = 'cc'
    extra_libs = ('-lrt',)

class GNUkFreebsd_64(Freebsd_64):
    DEFAULT_CC = 'cc'
    extra_libs = ('-lrt',)
