from rpython.rlib import jit
from pypy.interpreter.error import OperationError

def int_w(space, w_obj):
    try:
        return space.int_w(space.index(w_obj))
    except OperationError:
        return space.int_w(space.int(w_obj))

@jit.unroll_safe
def product(s):
    i = 1
    for x in s:
        i *= x
    return i

@jit.unroll_safe
def calc_strides(shape, dtype, order):
    strides = []
    backstrides = []
    s = 1
    shape_rev = shape[:]
    if order == 'C':
        shape_rev.reverse()
    for sh in shape_rev:
        slimit = max(sh, 1)
        strides.append(s * dtype.get_size())
        backstrides.append(s * (slimit - 1) * dtype.get_size())
        s *= slimit
    if order == 'C':
        strides.reverse()
        backstrides.reverse()
    return strides, backstrides
