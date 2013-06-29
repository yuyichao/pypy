import sys
import math
from rpython.rlib.rstring import StringBuilder
from rpython.rlib.objectmodel import specialize
from rpython.rlib import rfloat
from rpython.rtyper.lltypesystem import lltype, rffi
from pypy.interpreter.error import OperationError, operationerrfmt
from pypy.interpreter.gateway import unwrap_spec
from pypy.interpreter import unicodehelper
from rpython.rtyper.annlowlevel import llstr, hlunicode

OVF_DIGITS = len(str(sys.maxint))

def is_whitespace(ch):
    return ch == ' ' or ch == '\t' or ch == '\r' or ch == '\n'

# precomputing negative powers of 10 is MUCH faster than using e.g. math.pow
# at runtime
NEG_POW_10 = [10.0**-i for i in range(16)]
def neg_pow_10(x, exp):
    if exp >= len(NEG_POW_10):
        return 0.0
    return x * NEG_POW_10[exp]

def strslice2unicode_latin1(s, start, end):
    """
    Convert s[start:end] to unicode. s is supposed to be an RPython string
    encoded in latin-1, which means that the numeric value of each char is the
    same as the corresponding unicode code point.

    Internally it's implemented at the level of low-level helpers, to avoid
    the extra copy we would need if we take the actual slice first.
    
    No bound checking is done, use carefully.
    """
    from rpython.rtyper.annlowlevel import llstr, hlunicode
    from rpython.rtyper.lltypesystem.rstr import malloc, UNICODE
    from rpython.rtyper.lltypesystem.lltype import cast_primitive, UniChar
    length = end-start
    ll_s = llstr(s)
    ll_res = malloc(UNICODE, length)
    ll_res.hash = 0
    for i in range(length):
        ch = ll_s.chars[start+i]
        ll_res.chars[i] = cast_primitive(UniChar, ch)
    return hlunicode(ll_res)

TYPE_UNKNOWN = 0
TYPE_STRING = 1
class JSONDecoder(object):
    def __init__(self, space, s, precise_float=True):
        self.space = space
        self.s = s
        # we put our string in a raw buffer so:
        # 1) we automatically get the '\0' sentinel at the end of the string,
        #    which means that we never have to check for the "end of string"
        # 2) we can pass the buffer directly to strtod
        self.ll_chars = rffi.str2charp(s)
        self.end_ptr = lltype.malloc(rffi.CCHARPP.TO, 1, flavor='raw')
        self.pos = 0
        self.last_type = TYPE_UNKNOWN
        self.precise_float = precise_float

    def close(self):
        rffi.free_charp(self.ll_chars)
        lltype.free(self.end_ptr, flavor='raw')

    def getslice(self, start, end):
        assert start >= 0
        assert end >= 0
        return self.s[start:end]

    def skip_whitespace(self, i):
        while True:
            ch = self.ll_chars[i]
            if is_whitespace(ch):
                i+=1
            else:
                break
        return i

    @specialize.arg(1)
    def _raise(self, msg, *args):
        raise operationerrfmt(self.space.w_ValueError, msg, *args)

    def decode_any(self, i):
        i = self.skip_whitespace(i)
        ch = self.ll_chars[i]
        if ch == '"':
            return self.decode_string(i+1)
        elif ch == '[':
            return self.decode_array(i+1)
        elif ch == '{':
            return self.decode_object(i+1)
        elif ch == 'n':
            return self.decode_null(i+1)
        elif ch == 't':
            return self.decode_true(i+1)
        elif ch == 'f':
            return self.decode_false(i+1)
        elif ch.isdigit() or ch == '-':
            return self.decode_numeric(i)
        else:
            self._raise("No JSON object could be decoded: unexpected '%s' at char %d",
                        ch, self.pos)

    def decode_null(self, i):
        if (self.ll_chars[i]   == 'u' and
            self.ll_chars[i+1] == 'l' and
            self.ll_chars[i+2] == 'l'):
            self.pos = i+3
            return self.space.w_None
        self._raise("Error when decoding null at char %d", i)

    def decode_true(self, i):
        if (self.ll_chars[i]   == 'r' and
            self.ll_chars[i+1] == 'u' and
            self.ll_chars[i+2] == 'e'):
            self.pos = i+3
            return self.space.w_True
        self._raise("Error when decoding true at char %d", i)

    def decode_false(self, i):
        if (self.ll_chars[i]   == 'a' and
            self.ll_chars[i+1] == 'l' and
            self.ll_chars[i+2] == 's' and
            self.ll_chars[i+3] == 'e'):
            self.pos = i+4
            return self.space.w_False
        self._raise("Error when decoding false at char %d", i)

    def decode_numeric(self, i):
        w_res = self.decode_numeric_fast(i)
        if w_res is self.space.w_None:
            # possible overflow, reparse it
            return self.decode_numeric_slow(i)
        return w_res

    def decode_numeric_fast(self, i):
        start = i
        i, ovf_maybe, intval = self.parse_integer(i, allow_leading_0=False)
        #
        # check for the optional fractional part
        ch = self.ll_chars[i]
        if ch == '.':
            if not self.ll_chars[i+1].isdigit():
                self._raise("Expected digit at char %d", i+1)
            return self.parse_float(start, i, intval, ovf_maybe)
        elif ch == 'e' or ch == 'E':
            return self.parse_float(start, i, intval, ovf_maybe)
        elif ovf_maybe:
            # apparently we get a ~30% slowdown on my microbenchmark if we
            # return None instead of w_None, probably because the annotation
            # of the results geta can_be_None=True. We need to check if this
            # is still true also for the full pypy
            return self.space.w_None

        self.pos = i
        return self.space.wrap(intval)

    def parse_float(self, start, i, intval, ovf_maybe):
        if self.precise_float or ovf_maybe:
            return self.parse_float_precise(start)
        return self.parse_float_fast(i, intval)

    def parse_float_precise(self, i):
        from rpython.rlib import rdtoa
        start = rffi.ptradd(self.ll_chars, i)
        floatval = rdtoa.dg_strtod(start, self.end_ptr)
        diff = rffi.cast(rffi.LONG, self.end_ptr[0]) - rffi.cast(rffi.LONG, start)
        self.pos = i + diff
        return self.space.wrap(floatval)

    def parse_float_fast(self, i, intval):
        exp = 0
        frcval = 0
        ch = self.ll_chars[i]
        if ch == '.':
            i, frcval, frccount = self.parse_digits(i+1)
            frcval = neg_pow_10(frcval, frccount)
            ch = self.ll_chars[i]
        # check for the optional exponent part
        if ch == 'E' or ch == 'e':
            i, ovf_maybe, exp = self.parse_integer(i+1, allow_leading_0=True)
        #
        floatval = float(intval) + frcval
        if exp != 0:
            try:
                floatval = floatval * math.pow(10, exp)
            except OverflowError:
                floatval = rfloat.INFINITY
        self.pos = i
        return self.space.wrap(floatval)

    def decode_numeric_slow(self, i):
        is_float = False
        start = i
        if self.ll_chars[i] == '-':
            i += 1
        # skip the integral part
        while self.ll_chars[i].isdigit():
            i += 1
        #
        # skip the fractional part, if any
        if self.ll_chars[i] == '.':
            is_float = True
            i += 1
            while self.ll_chars[i].isdigit():
                i += 1
        #
        # skip the exponent part, if any
        if self.ll_chars[i] == 'e' or self.ll_chars[i] == 'E':
            is_float = True
            i += 1
            if self.ll_chars[i] == '+' or self.ll_chars[i] == '-':
                i += 1
            while self.ll_chars[i].isdigit():
                i += 1
        #
        self.pos = i
        s = self.getslice(start, i)
        if is_float:
            w_func = self.space.w_float
        else:
            w_func = self.space.w_int
        w_res = self.space.call_function(w_func, self.space.wrap(s))
        #assert w_res is not None # XXX check if this brings any speedup in pypy-c
        return w_res

    def parse_integer(self, i, allow_leading_0=False):
        "Parse a decimal number with an optional minus sign"
        start = i
        sign = 1
        if self.ll_chars[i] == '-':
            sign = -1
            i += 1
        elif self.ll_chars[i] == '+':
            i += 1
        elif not allow_leading_0 and self.ll_chars[i] == '0':
            i += 1
            return i, False, 0
        i, intval, _ = self.parse_digits(i)
        # if the number has more digits than OVF_DIGITS, it might have
        # overflowed
        ovf_maybe = (i-start >= OVF_DIGITS)
        return i, ovf_maybe, sign * intval
    parse_integer._always_inline_ = True

    def parse_digits(self, i):
        "Parse a sequence of digits as a decimal number. No sign allowed"
        intval = 0
        start = i 
        while True:
            ch = self.ll_chars[i]
            if ch.isdigit():
                intval = intval*10 + ord(ch)-ord('0')
                i += 1
            else:
                break
        count = i - start
        if count == 0:
            self._raise("Expected digit at char %d", i)
        return i, intval, count

    def decode_array(self, i):
        w_list = self.space.newlist([])
        start = i
        count = 0
        i = self.skip_whitespace(start)
        if self.ll_chars[i] == ']':
            self.pos = i+1
            return w_list
        #
        while True:
            w_item = self.decode_any(i)
            i = self.pos
            self.space.call_method(w_list, 'append', w_item)
            i = self.skip_whitespace(i)
            ch = self.ll_chars[i]
            i += 1
            if ch == ']':
                self.pos = i
                return w_list
            elif ch == ',':
                pass
            elif ch == '\0':
                self._raise("Unterminated array starting at char %d", start)
            else:
                self._raise("Unexpected '%s' when decoding array (char %d)",
                            ch, self.pos)


    def decode_object(self, i):
        start = i
        w_dict = self.space.newdict()
        #
        i = self.skip_whitespace(i)
        if self.ll_chars[i] == '}':
            self.pos = i+1
            return w_dict
        #
        while True:
            # parse a key: value
            self.last_type = TYPE_UNKNOWN
            w_name = self.decode_any(i)
            if self.last_type != TYPE_STRING:
                self._raise("Key name must be string for object starting at char %d", start)
            i = self.skip_whitespace(self.pos)
            ch = self.ll_chars[i]
            if ch != ':':
                self._raise("No ':' found at char %d", i)
            i += 1
            i = self.skip_whitespace(i)
            #
            w_value = self.decode_any(i)
            self.space.setitem(w_dict, w_name, w_value)
            i = self.skip_whitespace(self.pos)
            ch = self.ll_chars[i]
            i += 1
            if ch == '}':
                self.pos = i
                return w_dict
            elif ch == ',':
                pass
            elif ch == '\0':
                self._raise("Unterminated object starting at char %d", start)
            else:
                self._raise("Unexpected '%s' when decoding object (char %d)",
                            ch, self.pos)


    def decode_string(self, i):
        start = i
        bits = 0
        while True:
            # this loop is a fast path for strings which do not contain escape
            # characters
            ch = self.ll_chars[i]
            i += 1
            bits |= ord(ch)
            if ch == '"':
                if bits & 0x80:
                    # the 8th bit is set, it's an utf8 strnig
                    content_utf8 = self.getslice(start, i-1)
                    content_unicode = unicodehelper.decode_utf8(self.space, content_utf8)
                else:
                    # ascii only, fast path (ascii is a strict subset of
                    # latin1, and we already checked that all the chars are <
                    # 128)
                    content_unicode = strslice2unicode_latin1(self.s, start, i-1)
                self.last_type = TYPE_STRING
                self.pos = i
                return self.space.wrap(content_unicode)
            elif ch == '\\':
                content_so_far = self.getslice(start, i-1)
                self.pos = i-1
                return self.decode_string_escaped(start, content_so_far)
            elif ch == '\0':
                self._raise("Unterminated string starting at char %d", start)


    def decode_string_escaped(self, start, content_so_far):
        builder = StringBuilder(len(content_so_far)*2) # just an estimate
        builder.append(content_so_far)
        i = self.pos
        while True:
            ch = self.ll_chars[i]
            i += 1
            if ch == '"':
                content_utf8 = builder.build()
                content_unicode = unicodehelper.decode_utf8(self.space, content_utf8)
                self.last_type = TYPE_STRING
                self.pos = i
                return self.space.wrap(content_unicode)
            elif ch == '\\':
                i = self.decode_escape_sequence(i, builder)
            elif ch == '\0':
                self._raise("Unterminated string starting at char %d", start)
            else:
                builder.append_multiple_char(ch, 1) # we should implement append_char

    def decode_escape_sequence(self, i, builder):
        ch = self.ll_chars[i]
        i += 1
        put = builder.append_multiple_char
        if ch == '\\':  put('\\', 1)
        elif ch == '"': put('"' , 1)
        elif ch == '/': put('/' , 1)
        elif ch == 'b': put('\b', 1)
        elif ch == 'f': put('\f', 1)
        elif ch == 'n': put('\n', 1)
        elif ch == 'r': put('\r', 1)
        elif ch == 't': put('\t', 1)
        elif ch == 'u':
            return self.decode_escape_sequence_unicode(i, builder)
        else:
            self._raise("Invalid \\escape: %s (char %d)", ch, self.pos-1)
        return i

    def decode_escape_sequence_unicode(self, i, builder):
        # at this point we are just after the 'u' of the \u1234 sequence.
        start = i
        i += 4
        hexdigits = self.getslice(start, i)
        try:
            val = int(hexdigits, 16)
            if val & 0xfc00 == 0xd800:
                # surrogate pair
                val = self.decode_surrogate_pair(i, val)
                i += 6
        except ValueError:
            self._raise("Invalid \uXXXX escape (char %d)", i-1)
            return # help the annotator to know that we'll never go beyond
                   # this point
        #
        uchr = unichr(val)
        utf8_ch = unicodehelper.encode_utf8(self.space, uchr)
        builder.append(utf8_ch)
        return i

    def decode_surrogate_pair(self, i, highsurr):
        if self.ll_chars[i] != '\\' or self.ll_chars[i+1] != 'u':
            self._raise("Unpaired high surrogate at char %d", i)
        i += 2
        hexdigits = self.getslice(i, i+4)
        lowsurr = int(hexdigits, 16) # the possible ValueError is caugth by the caller
        return 0x10000 + (((highsurr - 0xd800) << 10) | (lowsurr - 0xdc00))

@unwrap_spec(precise_float=bool)
def loads(space, w_s, precise_float=True):
    if space.isinstance_w(w_s, space.w_unicode):
        raise OperationError(space.w_TypeError,
                             space.wrap("Expected utf8-encoded str, got unicode"))
    s = space.str_w(w_s)
    decoder = JSONDecoder(space, s, precise_float)
    try:
        w_res = decoder.decode_any(0)
        i = decoder.skip_whitespace(decoder.pos)
        if i < len(s):
            start = i
            end = len(s) - 1
            raise operationerrfmt(space.w_ValueError, "Extra data: char %d - %d", start, end)
        return w_res
    finally:
        decoder.close()
