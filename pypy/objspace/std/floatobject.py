import math
import operator

from pypy.interpreter.error import OperationError, oefmt
from pypy.objspace.std import newformat
from pypy.objspace.std.floattype import float_typedef, W_AbstractFloatObject
from pypy.objspace.std.intobject import HASH_BITS, HASH_MODULUS
from pypy.objspace.std.multimethod import FailedToImplementArgs
from pypy.objspace.std.model import registerimplementation, W_Object
from pypy.objspace.std.register_all import register_all
from pypy.objspace.std.longobject import W_LongObject, newlong_from_float
from pypy.objspace.std.noneobject import W_NoneObject
from rpython.rlib.rarithmetic import (
    LONG_BIT, intmask, ovfcheck_float_to_int, r_uint)
from rpython.rlib.rfloat import (
    isinf, isnan, isfinite, INFINITY, NAN, copysign, formatd,
    DTSF_ADD_DOT_0, float_as_rbigint_ratio)
from rpython.rlib.rbigint import rbigint
from rpython.rlib import rfloat
from rpython.tool.sourcetools import func_with_new_name

from pypy.objspace.std.intobject import W_IntObject

HASH_INF  = 314159
HASH_NAN  = 0

class W_FloatObject(W_AbstractFloatObject):
    """This is a implementation of the app-level 'float' type.
    The constructor takes an RPython float as an argument."""
    _immutable_fields_ = ['floatval']

    typedef = float_typedef

    def __init__(self, floatval):
        self.floatval = floatval

    def unwrap(self, space):
        return self.floatval

    def int_w(self, space, allow_conversion=True):
        self._typed_unwrap_error(space, "integer")

    def bigint_w(self, space, allow_conversion=True):
        self._typed_unwrap_error(space, "integer")

    def float_w(self, space, allow_conversion=True):
        return self.floatval

    def _float_w(self, space):
        return self.floatval

    def int(self, space):
        if (type(self) is not W_FloatObject and
            space.is_overloaded(self, space.w_float, '__int__')):
            return W_Object.int(self, space)
        try:
            value = ovfcheck_float_to_int(self.floatval)
        except OverflowError:
            return newlong_from_float(space, self.floatval)
        else:
            return space.newint(value)

    def __repr__(self):
        return "<W_FloatObject(%f)>" % self.floatval

registerimplementation(W_FloatObject)

# bool-to-float delegation
def delegate_Bool2Float(space, w_bool):
    return W_FloatObject(float(w_bool.intval))

# int-to-float delegation
def delegate_Int2Float(space, w_intobj):
    return W_FloatObject(float(w_intobj.intval))

# long-to-float delegation
def delegate_Long2Float(space, w_longobj):
    return W_FloatObject(w_longobj.tofloat(space))


# float__Float is supposed to do nothing, unless it has
# a derived float object, where it should return
# an exact one.
def float__Float(space, w_float1):
    if space.is_w(space.type(w_float1), space.w_float):
        return w_float1
    a = w_float1.floatval
    return W_FloatObject(a)

def trunc__Float(space, w_floatobj):
    whole = math.modf(w_floatobj.floatval)[1]
    try:
        value = ovfcheck_float_to_int(whole)
    except OverflowError:
        return newlong_from_float(space, whole)
    else:
        return space.newint(value)

def _char_from_hex(number):
    return "0123456789abcdef"[number]

TOHEX_NBITS = rfloat.DBL_MANT_DIG + 3 - (rfloat.DBL_MANT_DIG + 2) % 4

def float_hex__Float(space, w_float):
    value = w_float.floatval
    if not isfinite(value):
        return str__Float(space, w_float)
    if value == 0.0:
        if copysign(1., value) == -1.:
            return space.wrap("-0x0.0p+0")
        else:
            return space.wrap("0x0.0p+0")
    mant, exp = math.frexp(value)
    shift = 1 - max(rfloat.DBL_MIN_EXP - exp, 0)
    mant = math.ldexp(mant, shift)
    mant = abs(mant)
    exp -= shift
    result = ['\0'] * ((TOHEX_NBITS - 1) // 4 + 2)
    result[0] = _char_from_hex(int(mant))
    mant -= int(mant)
    result[1] = "."
    for i in range((TOHEX_NBITS - 1) // 4):
        mant *= 16.0
        result[i + 2] = _char_from_hex(int(mant))
        mant -= int(mant)
    if exp < 0:
        sign = "-"
    else:
        sign = "+"
    exp = abs(exp)
    s = ''.join(result)
    if value < 0.0:
        return space.wrap("-0x%sp%s%d" % (s, sign, exp))
    else:
        return space.wrap("0x%sp%s%d" % (s, sign, exp))

def float2string(x, code, precision):
    # we special-case explicitly inf and nan here
    if isfinite(x):
        s = formatd(x, code, precision, DTSF_ADD_DOT_0)
    elif isinf(x):
        if x > 0.0:
            s = "inf"
        else:
            s = "-inf"
    else:  # isnan(x):
        s = "nan"
    return s

def repr__Float(space, w_float):
    return space.wrap(float2string(w_float.floatval, 'r', 0))

str__Float = repr__Float

def format__Float_ANY(space, w_float, w_spec):
    return newformat.run_formatter(space, w_spec, "format_float", w_float)

# ____________________________________________________________
# A mess to handle all cases of float comparison without relying
# on delegation, which can unfortunately loose precision when
# casting an int or a long to a float.

def list_compare_funcs(declarator):
    for op in ['lt', 'le', 'eq', 'ne', 'gt', 'ge']:
        func, name = declarator(op)
        globals()[name] = func_with_new_name(func, name)

def _reverse(opname):
    if opname[0] == 'l': return 'g' + opname[1:]
    elif opname[0] == 'g': return 'l' + opname[1:]
    else: return opname


def declare_compare_bigint(opname):
    """Return a helper function that implements a float-bigint comparison."""
    op = getattr(operator, opname)
    #
    if opname == 'eq' or opname == 'ne':
        def do_compare_bigint(f1, b2):
            """f1 is a float.  b2 is a bigint."""
            if not isfinite(f1) or math.floor(f1) != f1:
                return opname == 'ne'
            b1 = rbigint.fromfloat(f1)
            res = b1.eq(b2)
            if opname == 'ne':
                res = not res
            return res
    else:
        def do_compare_bigint(f1, b2):
            """f1 is a float.  b2 is a bigint."""
            if not isfinite(f1):
                return op(f1, 0.0)
            if opname == 'gt' or opname == 'le':
                # 'float > long'   <==>  'ceil(float) > long'
                # 'float <= long'  <==>  'ceil(float) <= long'
                f1 = math.ceil(f1)
            else:
                # 'float < long'   <==>  'floor(float) < long'
                # 'float >= long'  <==>  'floor(float) >= long'
                f1 = math.floor(f1)
            b1 = rbigint.fromfloat(f1)
            return getattr(b1, opname)(b2)
    #
    return do_compare_bigint, 'compare_bigint_' + opname
list_compare_funcs(declare_compare_bigint)


def declare_cmp_float_float(opname):
    op = getattr(operator, opname)
    def f(space, w_float1, w_float2):
        f1 = w_float1.floatval
        f2 = w_float2.floatval
        return space.newbool(op(f1, f2))
    return f, opname + "__Float_Float"
list_compare_funcs(declare_cmp_float_float)

def declare_cmp_float_int(opname):
    op = getattr(operator, opname)
    compare = globals()['compare_bigint_' + opname]
    def f(space, w_float1, w_int2):
        f1 = w_float1.floatval
        i2 = w_int2.intval
        f2 = float(i2)
        if LONG_BIT > 32 and int(f2) != i2:
            res = compare(f1, rbigint.fromint(i2))
        else:
            res = op(f1, f2)
        return space.newbool(res)
    return f, opname + "__Float_Int"
list_compare_funcs(declare_cmp_float_int)

def declare_cmp_float_long(opname):
    compare = globals()['compare_bigint_' + opname]
    def f(space, w_float1, w_long2):
        f1 = w_float1.floatval
        b2 = w_long2.num
        return space.newbool(compare(f1, b2))
    return f, opname + "__Float_Long"
list_compare_funcs(declare_cmp_float_long)

def declare_cmp_int_float(opname):
    op = getattr(operator, opname)
    revcompare = globals()['compare_bigint_' + _reverse(opname)]
    def f(space, w_int1, w_float2):
        f2 = w_float2.floatval
        i1 = w_int1.intval
        f1 = float(i1)
        if LONG_BIT > 32 and int(f1) != i1:
            res = revcompare(f2, rbigint.fromint(i1))
        else:
            res = op(f1, f2)
        return space.newbool(res)
    return f, opname + "__Int_Float"
list_compare_funcs(declare_cmp_int_float)

def declare_cmp_long_float(opname):
    revcompare = globals()['compare_bigint_' + _reverse(opname)]
    def f(space, w_long1, w_float2):
        f2 = w_float2.floatval
        b1 = w_long1.num
        return space.newbool(revcompare(f2, b1))
    return f, opname + "__Long_Float"
list_compare_funcs(declare_cmp_long_float)


# ____________________________________________________________

def hash__Float(space, w_value):
    return space.wrap(_hash_float(space, w_value.floatval))

def _hash_float(space, v):
    if not isfinite(v):
        if isinf(v):
            return HASH_INF if v > 0 else -HASH_INF
        return HASH_NAN

    m, e = math.frexp(v)

    sign = 1
    if m < 0:
        sign = -1
        m = -m

    # process 28 bits at a time;  this should work well both for binary
    # and hexadecimal floating point.
    x = r_uint(0)
    while m:
        x = ((x << 28) & HASH_MODULUS) | x >> (HASH_BITS - 28)
        m *= 268435456.0  # 2**28
        e -= 28
        y = r_uint(m)  # pull out integer part
        m -= y
        x += y
        if x >= HASH_MODULUS:
            x -= HASH_MODULUS

    # adjust for the exponent;  first reduce it modulo HASH_BITS
    e = e % HASH_BITS if e >= 0 else HASH_BITS - 1 - ((-1 - e) % HASH_BITS)
    x = ((x << e) & HASH_MODULUS) | x >> (HASH_BITS - e)

    x = intmask(intmask(x) * sign)
    return -2 if x == -1 else x


def add__Float_Float(space, w_float1, w_float2):
    x = w_float1.floatval
    y = w_float2.floatval
    return W_FloatObject(x + y)

def sub__Float_Float(space, w_float1, w_float2):
    x = w_float1.floatval
    y = w_float2.floatval
    return W_FloatObject(x - y)

def mul__Float_Float(space, w_float1, w_float2):
    x = w_float1.floatval
    y = w_float2.floatval
    return W_FloatObject(x * y)

def div__Float_Float(space, w_float1, w_float2):
    x = w_float1.floatval
    y = w_float2.floatval
    if y == 0.0:
        raise FailedToImplementArgs(space.w_ZeroDivisionError, space.wrap("float division"))
    return W_FloatObject(x / y)

truediv__Float_Float = div__Float_Float

def floordiv__Float_Float(space, w_float1, w_float2):
    w_div, w_mod = _divmod_w(space, w_float1, w_float2)
    return w_div

def mod__Float_Float(space, w_float1, w_float2):
    x = w_float1.floatval
    y = w_float2.floatval
    if y == 0.0:
        raise FailedToImplementArgs(space.w_ZeroDivisionError, space.wrap("float modulo"))
    try:
        mod = math.fmod(x, y)
    except ValueError:
        mod = rfloat.NAN
    else:
        if mod:
            # ensure the remainder has the same sign as the denominator
            if (y < 0.0) != (mod < 0.0):
                mod += y
        else:
            # the remainder is zero, and in the presence of signed zeroes
            # fmod returns different results across platforms; ensure
            # it has the same sign as the denominator; we'd like to do
            # "mod = y * 0.0", but that may get optimized away
            mod = copysign(0.0, y)

    return W_FloatObject(mod)

def _divmod_w(space, w_float1, w_float2):
    x = w_float1.floatval
    y = w_float2.floatval
    if y == 0.0:
        raise FailedToImplementArgs(space.w_ZeroDivisionError, space.wrap("float modulo"))
    try:
        mod = math.fmod(x, y)
    except ValueError:
        return [W_FloatObject(rfloat.NAN), W_FloatObject(rfloat.NAN)]
    # fmod is typically exact, so vx-mod is *mathematically* an
    # exact multiple of wx.  But this is fp arithmetic, and fp
    # vx - mod is an approximation; the result is that div may
    # not be an exact integral value after the division, although
    # it will always be very close to one.
    div = (x - mod) / y
    if (mod):
        # ensure the remainder has the same sign as the denominator
        if ((y < 0.0) != (mod < 0.0)):
            mod += y
            div -= 1.0
    else:
        # the remainder is zero, and in the presence of signed zeroes
        # fmod returns different results across platforms; ensure
        # it has the same sign as the denominator; we'd like to do
        # "mod = wx * 0.0", but that may get optimized away
        mod *= mod  # hide "mod = +0" from optimizer
        if y < 0.0:
            mod = -mod
    # snap quotient to nearest integral value
    if div:
        floordiv = math.floor(div)
        if (div - floordiv > 0.5):
            floordiv += 1.0
    else:
        # div is zero - get the same sign as the true quotient
        div *= div  # hide "div = +0" from optimizers
        floordiv = div * x / y  # zero w/ sign of vx/wx

    return [W_FloatObject(floordiv), W_FloatObject(mod)]

def divmod__Float_Float(space, w_float1, w_float2):
    return space.newtuple(_divmod_w(space, w_float1, w_float2))

def pow__Float_Float_ANY(space, w_float1, w_float2, thirdArg):
    # This raises FailedToImplement in cases like overflow where a
    # (purely theoretical) big-precision float implementation would have
    # a chance to give a result, and directly OperationError for errors
    # that we want to force to be reported to the user.
    if not space.is_w(thirdArg, space.w_None):
        raise oefmt(space.w_TypeError, "pow() 3rd argument not allowed "
                    "unless all arguments are integers")
    x = w_float1.floatval
    y = w_float2.floatval

    try:
        result = _pow(space, x, y)
    except PowDomainError:
        # Negative numbers raised to fractional powers become complex
        return space.pow(space.newcomplex(x, 0.0),
                         space.newcomplex(y, 0.0),
                         thirdArg)
    return W_FloatObject(result)

class PowDomainError(ValueError):
    """Signals a negative number raised to a fractional power"""

def _pow(space, x, y):
    # Sort out special cases here instead of relying on pow()
    if y == 2.0:       # special case for performance:
        return x * x   # x * x is always correct
    if y == 0.0:
        # x**0 is 1, even 0**0
        return 1.0
    if isnan(x):
        # nan**y = nan, unless y == 0
        return x
    if isnan(y):
        # x**nan = nan, unless x == 1; x**nan = x
        if x == 1.0:
            return 1.0
        else:
            return y
    if isinf(y):
        # x**inf is: 0.0 if abs(x) < 1; 1.0 if abs(x) == 1; inf if
        # abs(x) > 1 (including case where x infinite)
        #
        # x**-inf is: inf if abs(x) < 1; 1.0 if abs(x) == 1; 0.0 if
        # abs(x) > 1 (including case where v infinite)
        x = abs(x)
        if x == 1.0:
            return 1.0
        elif (y > 0.0) == (x > 1.0):
            return INFINITY
        else:
            return 0.0
    if isinf(x):
        # (+-inf)**w is: inf for w positive, 0 for w negative; in oth
        # cases, we need to add the appropriate sign if w is an odd
        # integer.
        y_is_odd = math.fmod(abs(y), 2.0) == 1.0
        if y > 0.0:
            if y_is_odd:
                return x
            else:
                return abs(x)
        else:
            if y_is_odd:
                return copysign(0.0, x)
            else:
                return 0.0

    if x == 0.0:
        if y < 0.0:
            raise oefmt(space.w_ZeroDivisionError,
                        "0.0 cannot be raised to a negative power")

    negate_result = False
    # special case: "(-1.0) ** bignum" should not raise PowDomainError,
    # unlike "math.pow(-1.0, bignum)".  See http://mail.python.org/
    # -           pipermail/python-bugs-list/2003-March/016795.html
    if x < 0.0:
        if isnan(y):
            return NAN
        if math.floor(y) != y:
            raise PowDomainError
        # y is an exact integer, albeit perhaps a very large one.
        # Replace x by its absolute value and remember to negate the
        # pow result if y is odd.
        x = -x
        negate_result = math.fmod(abs(y), 2.0) == 1.0

    if x == 1.0:
        # (-1) ** large_integer also ends up here
        if negate_result:
            return -1.0
        else:
            return 1.0

    try:
        # We delegate to our implementation of math.pow() the error detection.
        z = math.pow(x,y)
    except OverflowError:
        raise FailedToImplementArgs(space.w_OverflowError,
                                    space.wrap("float power"))
    except ValueError:
        raise oefmt(space.w_ValueError, "float power")

    if negate_result:
        z = -z
    return z


def neg__Float(space, w_float1):
    return W_FloatObject(-w_float1.floatval)

def pos__Float(space, w_float):
    return float__Float(space, w_float)

def abs__Float(space, w_float):
    return W_FloatObject(abs(w_float.floatval))

def nonzero__Float(space, w_float):
    return space.newbool(w_float.floatval != 0.0)

def getnewargs__Float(space, w_float):
    return space.newtuple([W_FloatObject(w_float.floatval)])

def float_as_integer_ratio__Float(space, w_float):
    value = w_float.floatval
    try:
        num, den = float_as_rbigint_ratio(value)
    except OverflowError:
        raise oefmt(space.w_OverflowError,
                    "cannot pass infinity to as_integer_ratio()")
    except ValueError:
        raise oefmt(space.w_ValueError,
                    "cannot pass nan to as_integer_ratio()")

    w_num = space.newlong_from_rbigint(num)
    w_den = space.newlong_from_rbigint(den)
    # Try to return int
    return space.newtuple([space.int(w_num), space.int(w_den)])

def float_is_integer__Float(space, w_float):
    v = w_float.floatval
    if not rfloat.isfinite(v):
        return space.w_False
    return space.wrap(math.floor(v) == v)

from pypy.objspace.std import floattype
register_all(vars(), floattype)
