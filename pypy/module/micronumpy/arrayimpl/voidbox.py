
from pypy.module.micronumpy.arrayimpl.base import BaseArrayImplementation
from pypy.rlib.rawstorage import free_raw_storage, alloc_raw_storage

class VoidBoxStorage(BaseArrayImplementation):
    def __init__(self, size, dtype):
        self.storage = alloc_raw_storage(size)
        self.dtype = dtype

    def __del__(self):
        free_raw_storage(self.storage)
