import operator
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.baseobjspace import ObjSpace
from pypy.interpreter.function import Function, Method, FunctionWithFixedCode
from pypy.interpreter.argument import Arguments
from pypy.interpreter.typedef import default_identity_hash
from rpython.tool.sourcetools import compile2, func_with_new_name
from rpython.rlib.objectmodel import specialize
from rpython.rlib import jit

def object_getattribute(space):
    "Utility that returns the app-level descriptor object.__getattribute__."
    w_src, w_getattribute = space.lookup_in_type_where(space.w_object,
                                                       '__getattribute__')
    return w_getattribute
object_getattribute._annspecialcase_ = 'specialize:memo'

def object_setattr(space):
    "Utility that returns the app-level descriptor object.__setattr__."
    w_src, w_setattr = space.lookup_in_type_where(space.w_object,
                                                  '__setattr__')
    return w_setattr
object_setattr._annspecialcase_ = 'specialize:memo'

def object_delattr(space):
    "Utility that returns the app-level descriptor object.__delattr__."
    w_src, w_delattr = space.lookup_in_type_where(space.w_object,
                                                  '__delattr__')
    return w_delattr
object_delattr._annspecialcase_ = 'specialize:memo'

def object_hash(space):
    "Utility that returns the app-level descriptor object.__hash__."
    w_src, w_hash = space.lookup_in_type_where(space.w_object,
                                                  '__hash__')
    return w_hash
object_hash._annspecialcase_ = 'specialize:memo'

def type_eq(space):
    "Utility that returns the app-level descriptor type.__eq__."
    w_src, w_eq = space.lookup_in_type_where(space.w_type,
                                             '__eq__')
    return w_eq
type_eq._annspecialcase_ = 'specialize:memo'

def list_iter(space):
    "Utility that returns the app-level descriptor list.__iter__."
    w_src, w_iter = space.lookup_in_type_where(space.w_list,
                                               '__iter__')
    return w_iter
list_iter._annspecialcase_ = 'specialize:memo'

def tuple_iter(space):
    "Utility that returns the app-level descriptor tuple.__iter__."
    w_src, w_iter = space.lookup_in_type_where(space.w_tuple,
                                               '__iter__')
    return w_iter
tuple_iter._annspecialcase_ = 'specialize:memo'

def unicode_iter(space):
    "Utility that returns the app-level descriptor str.__iter__."
    w_src, w_iter = space.lookup_in_type_where(space.w_unicode,
                                               '__iter__')
    return w_iter
unicode_iter._annspecialcase_ = 'specialize:memo'

def raiseattrerror(space, w_obj, w_name, w_descr=None):
    # space.repr always returns an encodable string.
    if w_descr is None:
        raise oefmt(space.w_AttributeError,
                    "'%T' object has no attribute %R", w_obj, w_name)
    else:
        raise oefmt(space.w_AttributeError,
                    "'%T' object attribute %R is read-only", w_obj, w_name)

def get_attribute_name(space, w_obj, w_name):
    try:
        return space.str_w(w_name)
    except OperationError as e:
        if e.match(space, space.w_UnicodeEncodeError):
            raiseattrerror(space, w_obj, w_name)
        raise

def _same_class_w(space, w_obj1, w_obj2, w_typ1, w_typ2):
    return space.is_w(w_typ1, w_typ2)


class Object(object):
    def descr__getattribute__(space, w_obj, w_name):
        name = get_attribute_name(space, w_obj, w_name)
        w_descr = space.lookup(w_obj, name)
        if w_descr is not None:
            if space.is_data_descr(w_descr):
                # Only override if __get__ is defined, too, for compatibility
                # with CPython.
                w_get = space.lookup(w_descr, "__get__")
                if w_get is not None:
                    w_type = space.type(w_obj)
                    return space.get_and_call_function(w_get, w_descr, w_obj,
                                                       w_type)
        w_value = w_obj.getdictvalue(space, name)
        if w_value is not None:
            return w_value
        if w_descr is not None:
            return space.get(w_descr, w_obj)
        raiseattrerror(space, w_obj, w_name)

    def descr__setattr__(space, w_obj, w_name, w_value):
        name = get_attribute_name(space, w_obj, w_name)
        w_descr = space.lookup(w_obj, name)
        if w_descr is not None:
            if space.is_data_descr(w_descr):
                space.set(w_descr, w_obj, w_value)
                return
        if w_obj.setdictvalue(space, name, w_value):
            return
        raiseattrerror(space, w_obj, w_name, w_descr)

    def descr__delattr__(space, w_obj, w_name):
        name = get_attribute_name(space, w_obj, w_name)
        w_descr = space.lookup(w_obj, name)
        if w_descr is not None:
            if space.is_data_descr(w_descr):
                space.delete(w_descr, w_obj)
                return
        if w_obj.deldictvalue(space, name):
            return
        raiseattrerror(space, w_obj, w_name, w_descr)

    def descr__init__(space, w_obj, __args__):
        pass

contains_jitdriver = jit.JitDriver(name='contains',
        greens=['w_type'], reds='auto')

class DescrOperation(object):
    # This is meant to be a *mixin*.

    def is_data_descr(space, w_obj):
        return space.lookup(w_obj, '__set__') is not None

    def get_and_call_args(space, w_descr, w_obj, args):
        # a special case for performance and to avoid infinite recursion
        if isinstance(w_descr, Function):
            return w_descr.call_obj_args(w_obj, args)
        else:
            w_impl = space.get(w_descr, w_obj)
            return space.call_args(w_impl, args)

    def get_and_call_function(space, w_descr, w_obj, *args_w):
        typ = type(w_descr)
        # a special case for performance and to avoid infinite recursion
        if typ is Function or typ is FunctionWithFixedCode:
            # isinstance(typ, Function) would not be correct here:
            # for a BuiltinFunction we must not use that shortcut, because a
            # builtin function binds differently than a normal function
            # see test_builtin_as_special_method_is_not_bound
            # in interpreter/test/test_function.py

            # the fastcall paths are purely for performance, but the resulting
            # increase of speed is huge
            return w_descr.funccall(w_obj, *args_w)
        else:
            args = Arguments(space, list(args_w))
            w_impl = space.get(w_descr, w_obj)
            return space.call_args(w_impl, args)

    def call_args(space, w_obj, args):
        # two special cases for performance
        if isinstance(w_obj, Function):
            return w_obj.call_args(args)
        if isinstance(w_obj, Method):
            return w_obj.call_args(args)
        w_descr = space.lookup(w_obj, '__call__')
        if w_descr is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object is not callable", w_obj)
        return space.get_and_call_args(w_descr, w_obj, args)

    def get(space, w_descr, w_obj, w_type=None):
        w_get = space.lookup(w_descr, '__get__')
        if w_get is None:
            return w_descr
        if w_type is None:
            w_type = space.type(w_obj)
        return space.get_and_call_function(w_get, w_descr, w_obj, w_type)

    def set(space, w_descr, w_obj, w_val):
        w_set = space.lookup(w_descr, '__set__')
        if w_set is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object is not a descriptor with set", w_descr)
        return space.get_and_call_function(w_set, w_descr, w_obj, w_val)

    def delete(space, w_descr, w_obj):
        w_delete = space.lookup(w_descr, '__delete__')
        if w_delete is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object is not a descriptor with delete", w_descr)
        return space.get_and_call_function(w_delete, w_descr, w_obj)

    def getattr(space, w_obj, w_name):
        # may be overridden in StdObjSpace
        w_descr = space.lookup(w_obj, '__getattribute__')
        return space._handle_getattribute(w_descr, w_obj, w_name)

    def _handle_getattribute(space, w_descr, w_obj, w_name):
        try:
            if w_descr is None:   # obscure case
                raise OperationError(space.w_AttributeError, space.w_None)
            return space.get_and_call_function(w_descr, w_obj, w_name)
        except OperationError, e:
            if not e.match(space, space.w_AttributeError):
                raise
            w_descr = space.lookup(w_obj, '__getattr__')
            if w_descr is None:
                raise
            return space.get_and_call_function(w_descr, w_obj, w_name)

    def setattr(space, w_obj, w_name, w_val):
        w_descr = space.lookup(w_obj, '__setattr__')
        if w_descr is None:
            raise oefmt(space.w_AttributeError,
                        "'%T' object is readonly", w_obj)
        return space.get_and_call_function(w_descr, w_obj, w_name, w_val)

    def delattr(space, w_obj, w_name):
        w_descr = space.lookup(w_obj, '__delattr__')
        if w_descr is None:
            raise oefmt(space.w_AttributeError,
                        "'%T' object does not support attribute removal",
                        w_obj)
        return space.get_and_call_function(w_descr, w_obj, w_name)

    def is_true(space, w_obj):
        w_descr = space.lookup(w_obj, "__bool__")
        if w_descr is None:
            w_descr = space.lookup(w_obj, "__len__")
            if w_descr is None:
                return True
            # call __len__
            w_res = space.get_and_call_function(w_descr, w_obj)
            return space._check_len_result(w_res) != 0
        # call __bool__
        w_res = space.get_and_call_function(w_descr, w_obj)
        # more shortcuts for common cases
        if space.is_w(w_res, space.w_False):
            return False
        if space.is_w(w_res, space.w_True):
            return True
        w_restype = space.type(w_res)
        # Note there is no check for bool here because the only possible
        # instances of bool are w_False and w_True, which are checked above.
        raise oefmt(space.w_TypeError,
                    "__bool__ should return bool, returned %T", w_obj)

    def nonzero(space, w_obj):
        if space.is_true(w_obj):
            return space.w_True
        else:
            return space.w_False

    def len(space, w_obj):
        w_descr = space.lookup(w_obj, '__len__')
        if w_descr is None:
            raise oefmt(space.w_TypeError, "'%T' has no length", w_obj)
        w_res = space.get_and_call_function(w_descr, w_obj)
        return space.wrap(space._check_len_result(w_res))

    def _check_len_result(space, w_obj):
        # Will complain if result is too big.
        result = space.int_w(w_obj, allow_conversion=False)
        if result < 0:
            raise oefmt(space.w_ValueError, "__len__() should return >= 0")
        return result

    def iter(space, w_obj):
        w_descr = space.lookup(w_obj, '__iter__')
        if w_descr is None:
            w_descr = space.lookup(w_obj, '__getitem__')
            if w_descr is None:
                raise oefmt(space.w_TypeError,
                            "'%T' object is not iterable", w_obj)
            return space.newseqiter(w_obj)
        w_iter = space.get_and_call_function(w_descr, w_obj)
        w_next = space.lookup(w_iter, '__next__')
        if w_next is None:
            raise oefmt(space.w_TypeError, "iter() returned non-iterator")
        return w_iter

    def next(space, w_obj):
        w_descr = space.lookup(w_obj, '__next__')
        if w_descr is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object is not an iterator", w_obj)
        return space.get_and_call_function(w_descr, w_obj)

    def getitem(space, w_obj, w_key):
        w_descr = space.lookup(w_obj, '__getitem__')
        if w_descr is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object is not subscriptable", w_obj)
        return space.get_and_call_function(w_descr, w_obj, w_key)

    def setitem(space, w_obj, w_key, w_val):
        w_descr = space.lookup(w_obj, '__setitem__')
        if w_descr is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object does not support item assignment", w_obj)
        return space.get_and_call_function(w_descr, w_obj, w_key, w_val)

    def delitem(space, w_obj, w_key):
        w_descr = space.lookup(w_obj, '__delitem__')
        if w_descr is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object does not support item deletion", w_obj)
        return space.get_and_call_function(w_descr, w_obj, w_key)

    def format(space, w_obj, w_format_spec):
        w_descr = space.lookup(w_obj, '__format__')
        if w_descr is None:
            raise oefmt(space.w_TypeError,
                        "'%T' object does not define __format__", w_obj)
        w_res = space.get_and_call_function(w_descr, w_obj, w_format_spec)
        if not space.isinstance_w(w_res, space.w_unicode):
            raise oefmt(space.w_TypeError,
                        "%T.__format__ must return string, not %T",
                        w_obj, w_res)
        return w_res

    def pow(space, w_obj1, w_obj2, w_obj3):
        w_typ1 = space.type(w_obj1)
        w_typ2 = space.type(w_obj2)
        w_left_src, w_left_impl = space.lookup_in_type_where(w_typ1, '__pow__')
        if space.is_w(w_typ1, w_typ2):
            w_right_impl = None
        else:
            w_right_src, w_right_impl = space.lookup_in_type_where(w_typ2, '__rpow__')
            # sse binop_impl
            if (w_left_src is not w_right_src
                and space.is_true(space.issubtype(w_typ2, w_typ1))):
                if (w_left_src and w_right_src and
                    not space.abstract_issubclass_w(w_left_src, w_right_src) and
                    not space.abstract_issubclass_w(w_typ1, w_right_src)):
                    w_obj1, w_obj2 = w_obj2, w_obj1
                    w_left_impl, w_right_impl = w_right_impl, w_left_impl
        if w_left_impl is not None:
            if space.is_w(w_obj3, space.w_None):
                w_res = space.get_and_call_function(w_left_impl, w_obj1, w_obj2)
            else:
                w_res = space.get_and_call_function(w_left_impl, w_obj1, w_obj2, w_obj3)
            if _check_notimplemented(space, w_res):
                return w_res
        if w_right_impl is not None:
            if space.is_w(w_obj3, space.w_None):
                w_res = space.get_and_call_function(w_right_impl, w_obj2, w_obj1)
            else:
                w_res = space.get_and_call_function(w_right_impl, w_obj2, w_obj1,
                                                   w_obj3)
            if _check_notimplemented(space, w_res):
                return w_res

        raise oefmt(space.w_TypeError, "operands do not support **")

    def inplace_pow(space, w_lhs, w_rhs):
        w_impl = space.lookup(w_lhs, '__ipow__')
        if w_impl is not None:
            w_res = space.get_and_call_function(w_impl, w_lhs, w_rhs)
            if _check_notimplemented(space, w_res):
                return w_res
        return space.pow(w_lhs, w_rhs, space.w_None)

    def contains(space, w_container, w_item):
        w_descr = space.lookup(w_container, '__contains__')
        if w_descr is not None:
            w_result = space.get_and_call_function(w_descr, w_container, w_item)
            return space.nonzero(w_result)
        return space.sequence_contains(w_container, w_item)

    def sequence_contains(space, w_container, w_item):
        w_iter = space.iter(w_container)
        w_type = space.type(w_iter)
        while 1:
            contains_jitdriver.jit_merge_point(w_type=w_type)
            try:
                w_next = space.next(w_iter)
            except OperationError, e:
                if not e.match(space, space.w_StopIteration):
                    raise
                return space.w_False
            if space.eq_w(w_next, w_item):
                return space.w_True

    def sequence_count(space, w_container, w_item):
        w_iter = space.iter(w_container)
        count = 0
        while 1:
            try:
                w_next = space.next(w_iter)
            except OperationError, e:
                if not e.match(space, space.w_StopIteration):
                    raise
                return space.wrap(count)
            if space.eq_w(w_next, w_item):
                count += 1

    def sequence_index(space, w_container, w_item):
        w_iter = space.iter(w_container)
        index = 0
        while 1:
            try:
                w_next = space.next(w_iter)
            except OperationError, e:
                if not e.match(space, space.w_StopIteration):
                    raise
                raise oefmt(space.w_ValueError,
                            "sequence.index(x): x not in sequence")
            if space.eq_w(w_next, w_item):
                return space.wrap(index)
            index += 1

    def hash(space, w_obj):
        w_hash = space.lookup(w_obj, '__hash__')
        if w_hash is None:
            # xxx there used to be logic about "do we have __eq__ or __cmp__"
            # here, but it does not really make sense, as 'object' has a
            # default __hash__.  This path should only be taken under very
            # obscure circumstances.
            return default_identity_hash(space, w_obj)
        if space.is_w(w_hash, space.w_None):
            raise oefmt(space.w_TypeError,
                        "'%T' objects are unhashable", w_obj)
        w_result = space.get_and_call_function(w_hash, w_obj)
        if not space.isinstance_w(w_result, space.w_int):
            raise oefmt(space.w_TypeError,
                        "__hash__ method should return an integer")

        from pypy.objspace.std.intobject import (
            W_AbstractIntObject, W_IntObject)
        if type(w_result) is W_IntObject:
            return w_result
        elif isinstance(w_result, W_IntObject):
            return space.wrap(space.int_w(w_result))
        # a non W_IntObject int, assume long-like
        assert isinstance(w_result, W_AbstractIntObject)
        return w_result.descr_hash(space)

    def userdel(space, w_obj):
        w_del = space.lookup(w_obj, '__del__')
        if w_del is not None:
            space.get_and_call_function(w_del, w_obj)

    def issubtype(space, w_sub, w_type):
        return space._type_issubtype(w_sub, w_type)

    @specialize.arg_or_var(2)
    def isinstance_w(space, w_inst, w_type):
        return space._type_isinstance(w_inst, w_type)

    @specialize.arg_or_var(2)
    def isinstance(space, w_inst, w_type):
        return space.wrap(space.isinstance_w(w_inst, w_type))

    def issubtype_allow_override(space, w_sub, w_type):
        w_check = space.lookup(w_type, "__subclasscheck__")
        if w_check is None:
            raise oefmt(space.w_TypeError, "issubclass not supported here")
        return space.get_and_call_function(w_check, w_type, w_sub)

    def isinstance_allow_override(space, w_inst, w_type):
        w_check = space.lookup(w_type, "__instancecheck__")
        if w_check is not None:
            return space.get_and_call_function(w_check, w_type, w_inst)
        else:
            return space.isinstance(w_inst, w_type)


# helpers

def _check_notimplemented(space, w_obj):
    return not space.is_w(w_obj, space.w_NotImplemented)

def _invoke_binop(space, w_impl, w_obj1, w_obj2):
    if w_impl is not None:
        w_res = space.get_and_call_function(w_impl, w_obj1, w_obj2)
        if _check_notimplemented(space, w_res):
            return w_res
    return None


# regular methods def helpers

def _make_binop_impl(symbol, specialnames):
    left, right = specialnames
    errormsg = "unsupported operand type(s) for %s: '%%N' and '%%N'" % (
        symbol.replace('%', '%%'),)

    def binop_impl(space, w_obj1, w_obj2):
        w_typ1 = space.type(w_obj1)
        w_typ2 = space.type(w_obj2)
        w_left_src, w_left_impl = space.lookup_in_type_where(w_typ1, left)
        if space.is_w(w_typ1, w_typ2):
            w_right_impl = None
        else:
            w_right_src, w_right_impl = space.lookup_in_type_where(w_typ2, right)
            # the logic to decide if the reverse operation should be tried
            # before the direct one is very obscure.  For now, and for
            # sanity reasons, we just compare the two places where the
            # __xxx__ and __rxxx__ methods where found by identity.
            # Note that space.is_w() is potentially not happy if one of them
            # is None...
            if w_left_src is not w_right_src:    # XXX
                # -- end of bug compatibility
                if space.is_true(space.issubtype(w_typ2, w_typ1)):
                    if (w_left_src and w_right_src and
                        not space.abstract_issubclass_w(w_left_src, w_right_src) and
                        not space.abstract_issubclass_w(w_typ1, w_right_src)):
                        w_obj1, w_obj2 = w_obj2, w_obj1
                        w_left_impl, w_right_impl = w_right_impl, w_left_impl

        w_res = _invoke_binop(space, w_left_impl, w_obj1, w_obj2)
        if w_res is not None:
            return w_res
        w_res = _invoke_binop(space, w_right_impl, w_obj2, w_obj1)
        if w_res is not None:
            return w_res
        raise oefmt(space.w_TypeError, errormsg, w_typ1, w_typ2)

    return func_with_new_name(binop_impl, "binop_%s_impl"%left.strip('_'))

def _invoke_comparison(space, w_descr, w_obj1, w_obj2):
    if w_descr is not None:
        # a special case for performance (see get_and_call_function) but
        # also avoids binding via __get__ when unnecessary; in
        # particular when w_obj1 is None, __get__(None, type(None))
        # won't actually bind =]
        typ = type(w_descr)
        if typ is Function or typ is FunctionWithFixedCode:
            w_res = w_descr.funccall(w_obj1, w_obj2)
        else:
            try:
                w_impl = space.get(w_descr, w_obj1)
            except OperationError as e:
                # see testForExceptionsRaisedInInstanceGetattr2 in
                # test_class
                if not e.match(space, space.w_AttributeError):
                    raise
                return None
            else:
                w_res = space.call_function(w_impl, w_obj2)
        if _check_notimplemented(space, w_res):
            return w_res
    return None

def _make_comparison_impl(symbol, specialnames):
    left, right = specialnames
    op = getattr(operator, left)
    def comparison_impl(space, w_obj1, w_obj2):
        w_typ1 = space.type(w_obj1)
        w_typ2 = space.type(w_obj2)
        w_left_src, w_left_impl = space.lookup_in_type_where(w_typ1, left)
        w_first = w_obj1
        w_second = w_obj2

        w_right_src, w_right_impl = space.lookup_in_type_where(w_typ2,right)
        if space.is_w(w_typ1, w_typ2):
            # if the type is the same, then don't reverse: try
            # left first, right next.
            pass
        elif space.is_true(space.issubtype(w_typ2, w_typ1)):
            # if typ2 is a subclass of typ1.
            w_obj1, w_obj2 = w_obj2, w_obj1
            w_left_impl, w_right_impl = w_right_impl, w_left_impl

        w_res = _invoke_comparison(space, w_left_impl, w_obj1, w_obj2)
        if w_res is not None:
            return w_res
        w_res = _invoke_comparison(space, w_right_impl, w_obj2, w_obj1)
        if w_res is not None:
            return w_res
        #
        # we did not find any special method, let's do the default logic for
        # == and !=
        if left == '__eq__':
            if space.is_w(w_obj1, w_obj2):
                return space.w_True
            else:
                return space.w_False
        elif left == '__ne__':
            return space.not_(space.eq(w_obj1, w_obj2))
        #
        # if we arrived here, they are unorderable
        raise oefmt(space.w_TypeError,
                    "unorderable types: %T %s %T", w_obj1, symbol, w_obj2)

    return func_with_new_name(comparison_impl, 'comparison_%s_impl'%left.strip('_'))

def _make_inplace_impl(symbol, specialnames):
    specialname, = specialnames
    assert specialname.startswith('__i') and specialname.endswith('__')
    noninplacespacemethod = specialname[3:-2]
    if noninplacespacemethod in ['or', 'and']:
        noninplacespacemethod += '_'     # not too clean
    def inplace_impl(space, w_lhs, w_rhs):
        w_impl = space.lookup(w_lhs, specialname)
        if w_impl is not None:
            w_res = space.get_and_call_function(w_impl, w_lhs, w_rhs)
            if _check_notimplemented(space, w_res):
                return w_res
        # XXX fix the error message we get here
        return getattr(space, noninplacespacemethod)(w_lhs, w_rhs)

    return func_with_new_name(inplace_impl, 'inplace_%s_impl'%specialname.strip('_'))

def _make_unaryop_impl(symbol, specialnames):
    specialname, = specialnames
    errormsg = "unsupported operand type for unary %s: '%%T'" % symbol
    def unaryop_impl(space, w_obj):
        w_impl = space.lookup(w_obj, specialname)
        if w_impl is None:
            raise oefmt(space.w_TypeError, errormsg, w_obj)
        return space.get_and_call_function(w_impl, w_obj)
    return func_with_new_name(unaryop_impl, 'unaryop_%s_impl'%specialname.strip('_'))

# the following seven operations are really better to generate with
# string-templating (and maybe we should consider this for
# more of the above manually-coded operations as well)

for targetname, specialname, checkerspec in [
    ('index', '__index__', ("space.w_int",)),
    ('float', '__float__', ("space.w_float",))]:

    l = ["space.isinstance_w(w_result, %s)" % x
                for x in checkerspec]
    checker = " or ".join(l)
    if targetname == 'index':
        msg = "'%%T' object cannot be interpreted as an integer"
    else:
        msg = "unsupported operand type for %(targetname)s(): '%%T'"
    msg = msg % locals()
    source = """if 1:
        def %(targetname)s(space, w_obj):
            w_impl = space.lookup(w_obj, %(specialname)r)
            if w_impl is None:
                raise oefmt(space.w_TypeError,
                            %(msg)r,
                            w_obj)
            w_result = space.get_and_call_function(w_impl, w_obj)

            if %(checker)s:
                return w_result
            raise oefmt(space.w_TypeError,
                        "%(specialname)s returned non-%(targetname)s (type "
                        "'%%T')", w_result)
        assert not hasattr(DescrOperation, %(targetname)r)
        DescrOperation.%(targetname)s = %(targetname)s
        del %(targetname)s
        \n""" % locals()
    exec compile2(source)

for targetname, specialname in [
    ('str', '__str__'),
    ('repr', '__repr__')]:

    source = """if 1:
        def %(targetname)s(space, w_obj):
            w_impl = space.lookup(w_obj, %(specialname)r)
            if w_impl is None:
                raise oefmt(space.w_TypeError,
                            "unsupported operand type for %(targetname)s(): "
                            "'%%T'", w_obj)
            w_result = space.get_and_call_function(w_impl, w_obj)
            if space.isinstance_w(w_result, space.w_unicode):
                return w_result

            raise oefmt(space.w_TypeError,
                        "%(specialname)s returned non-%(targetname)s (type "
                        "'%%T')", w_result)
        assert not hasattr(DescrOperation, %(targetname)r)
        DescrOperation.%(targetname)s = %(targetname)s
        del %(targetname)s
        \n""" % locals()
    exec compile2(source)

# add default operation implementations for all still missing ops

for _name, _symbol, _arity, _specialnames in ObjSpace.MethodTable:
    if not hasattr(DescrOperation, _name):
        _impl_maker = None
        if _arity == 2 and _name in ['lt', 'le', 'gt', 'ge', 'ne', 'eq']:
            #print "comparison", _specialnames
            _impl_maker = _make_comparison_impl
        elif _arity == 2 and _name.startswith('inplace_'):
            #print "inplace", _specialnames
            _impl_maker = _make_inplace_impl
        elif _arity == 2 and len(_specialnames) == 2:
            #print "binop", _specialnames
            _impl_maker = _make_binop_impl
        elif _arity == 1 and len(_specialnames) == 1 and _name != 'int':
            #print "unaryop", _specialnames
            _impl_maker = _make_unaryop_impl
        if _impl_maker:
            setattr(DescrOperation,_name,_impl_maker(_symbol,_specialnames))
        elif _name not in ['is_', 'id','type','issubtype', 'int',
                           # not really to be defined in DescrOperation
                           'ord', 'unichr', 'unicode']:
            raise Exception, "missing def for operation %s" % _name
