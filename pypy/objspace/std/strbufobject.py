from pypy.objspace.std.bytesobject import W_AbstractBytesObject, W_BytesObject
from rpython.rlib.rstring import StringBuilder
from pypy.interpreter.buffer import Buffer

class W_StringBufferObject(W_AbstractBytesObject):
    w_str = None

    def __init__(self, builder):
        self.builder = builder             # StringBuilder
        self.length = builder.getlength()

    def force(self):
        if self.w_str is None:
            s = self.builder.build()
            if self.length < len(s):
                s = s[:self.length]
            self.w_str = W_BytesObject(s)
            return s
        else:
            return self.w_str._value

    def __repr__(w_self):
        """ representation for debugging purposes """
        return "%s(%r[:%d])" % (
            w_self.__class__.__name__, w_self.builder, w_self.length)

    def unwrap(self, space):
        return self.force()

    def str_w(self, space):
        return self.force()

W_StringBufferObject.typedef = W_BytesObject.typedef

# ____________________________________________________________

def joined2(str1, str2):
    builder = StringBuilder()
    builder.append(str1)
    builder.append(str2)
    return W_StringBufferObject(builder)

# ____________________________________________________________

def len__StringBuffer(space, w_self):
    return space.wrap(w_self.length)

def add__StringBuffer_Bytes(space, w_self, w_other):
    if w_self.builder.getlength() != w_self.length:
        builder = StringBuilder()
        builder.append(w_self.force())
    else:
        builder = w_self.builder
    builder.append(w_other._value)
    return W_StringBufferObject(builder)

def str__StringBuffer(space, w_self):
    # you cannot get subclasses of W_StringBufferObject here
    assert type(w_self) is W_StringBufferObject
    return w_self

from pypy.objspace.std import bytesobject
