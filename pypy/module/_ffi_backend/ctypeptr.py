"""
Pointers.
"""

from pypy.interpreter.error import operationerrfmt
from pypy.rpython.lltypesystem import rffi
from pypy.rlib.objectmodel import keepalive_until_here

from pypy.module._ffi_backend.ctypeobj import W_CType
from pypy.module._ffi_backend.ctypeprim import W_CTypePrimitiveChar
from pypy.module._ffi_backend import cdataobj, misc


class W_CTypePtrOrArray(W_CType):

    def __init__(self, space, size, extra, extra_position, ctitem):
        name, name_position = ctitem.insert_name(extra, extra_position)
        W_CType.__init__(self, space, size, name, name_position)
        self.ctitem = ctitem


class W_CTypePointer(W_CTypePtrOrArray):

    def __init__(self, space, ctitem):
        size = rffi.sizeof(rffi.VOIDP)
        from pypy.module._ffi_backend import ctypearray
        if isinstance(ctitem, ctypearray.W_CTypeArray):
            extra = "(*)"    # obscure case: see test_array_add
        else:
            extra = " *"
        W_CTypePtrOrArray.__init__(self, space, size, extra, 2, ctitem)

    def str(self, cdataobj):
        if isinstance(self.ctitem, W_CTypePrimitiveChar):
            if not cdataobj._cdata:
                space = self.space
                raise operationerrfmt(space.w_RuntimeError,
                                      "cannot use str() on %s",
                                      space.str_w(cdataobj.repr()))
            s = rffi.charp2str(cdataobj._cdata)
            keepalive_until_here(cdataobj)
            return self.space.wrap(s)
        return W_CTypePtrOrArray.str(self, cdataobj)

    def cast(self, w_ob):
        space = self.space
        ob = space.interpclass_w(w_ob)
        if (isinstance(ob, cdataobj.W_CData) and
                isinstance(ob.ctype, W_CTypePtrOrArray)):
            value = ob._cdata
        else:
            value = misc.as_unsigned_long_long(space, w_ob, strict=False)
            value = rffi.cast(rffi.CCHARP, value)
        return cdataobj.W_CData(space, value, self)

    def newp(self, w_init):
        from pypy.module._ffi_backend import ctypeprim
        space = self.space
        ctitem = self.ctitem
        datasize = ctitem.size
        if datasize < 0:
            raise operationerrfmt(space.w_TypeError,
                "cannot instantiate ctype '%s' of unknown size",
                                  self.name)
        if isinstance(ctitem, W_CTypePrimitiveChar):
            datasize *= 2       # forcefully add a null character
        cdata = cdataobj.W_CDataOwn(space, datasize, self)
        if not space.is_w(w_init, space.w_None):
            ctitem.convert_from_object(cdata._cdata, w_init)
            keepalive_until_here(cdata)
        return cdata

    def _check_subscript_index(self, w_cdata, i):
        if isinstance(w_cdata, cdataobj.W_CDataOwn) and i != 0:
            space = self.space
            raise operationerrfmt(space.w_IndexError,
                                  "cdata '%s' can only be indexed by 0",
                                  self.name)

    def add(self, cdata, i):
        space = self.space
        ctitem = self.ctitem
        if ctitem.size < 0:
            raise operationerrfmt(space.w_TypeError,
                                  "ctype '%s' points to items of unknown size",
                                  self.name)
        p = rffi.ptradd(cdata, i * self.ctitem.size)
        return cdataobj.W_CData(space, p, self)

    def _alignof(self):
        from pypy.module._ffi_backend import newtype
        return newtype.alignment_of_pointer

    def convert_to_object(self, cdata):
        ptrdata = rffi.cast(rffi.CCHARPP, cdata)[0]
        return cdataobj.W_CData(self.space, ptrdata, self)

    def convert_from_object(self, cdata, w_ob):
        space = self.space
        ob = space.interpclass_w(w_ob)
        if not isinstance(ob, cdataobj.W_CData):
            raise self._convert_error("compatible pointer", w_ob)
        otherctype = ob.ctype
        if (isinstance(otherctype, W_CTypePtrOrArray) and
            (self.ctitem.cast_anything or
             otherctype.ctitem.cast_anything or
             self.ctitem is otherctype.ctitem)):
            pass    # compatible types
        else:
            raise self._convert_error("compatible pointer", w_ob)

        rffi.cast(rffi.CCHARPP, cdata)[0] = ob._cdata
