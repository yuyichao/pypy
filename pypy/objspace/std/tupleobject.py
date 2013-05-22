import sys
from pypy.interpreter import gateway
from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.error import OperationError
from pypy.interpreter.gateway import interp2app, interpindirect2app
from pypy.objspace.std import slicetype
from pypy.objspace.std.inttype import wrapint
from pypy.objspace.std.sliceobject import W_SliceObject, normalize_simple_slice
from pypy.objspace.std.stdtypedef import StdTypeDef
from pypy.objspace.std.util import negate
from rpython.rlib import jit
from rpython.rlib.debug import make_sure_not_resized
from rpython.rlib.rarithmetic import intmask


UNROLL_CUTOFF = 10


def _unroll_condition(self):
    return jit.loop_unrolling_heuristic(self, self.length(), UNROLL_CUTOFF)


def _unroll_condition_cmp(self, space, other):
    return (jit.loop_unrolling_heuristic(self, self.length(), UNROLL_CUTOFF) or
            jit.loop_unrolling_heuristic(other, other.length(), UNROLL_CUTOFF))


class W_AbstractTupleObject(W_Root):
    __slots__ = ()

    def __repr__(self):
        """representation for debugging purposes"""
        reprlist = [repr(w_item) for w_item in self.tolist()]
        return "%s(%s)" % (self.__class__.__name__, ', '.join(reprlist))

    def unwrap(self, space):
        items = [space.unwrap(w_item) for w_item in self.tolist()]
        return tuple(items)

    def tolist(self):
        """Returns the items, as a fixed-size list."""
        raise NotImplementedError

    def getitems_copy(self):
        """Returns a copy of the items, as a resizable list."""
        raise NotImplementedError

    def length(self):
        raise NotImplementedError

    def getitem(self, space, item):
        raise NotImplementedError

    def descr_len(self, space):
        result = self.length()
        return wrapint(space, result)

    def descr_iter(self, space):
        from pypy.objspace.std import iterobject
        return iterobject.W_FastTupleIterObject(self, self.tolist())

    @staticmethod
    def descr_new(space, w_tupletype, w_sequence=None):
        if w_sequence is None:
            tuple_w = []
        elif (space.is_w(w_tupletype, space.w_tuple) and
              space.is_w(space.type(w_sequence), space.w_tuple)):
            return w_sequence
        else:
            tuple_w = space.fixedview(w_sequence)
        w_obj = space.allocate_instance(W_TupleObject, w_tupletype)
        W_TupleObject.__init__(w_obj, tuple_w)
        return w_obj

    def descr_repr(self, space):
        items = self.tolist()
        if len(items) == 1:
            return space.wrap(u"(" + space.unicode_w(space.repr(items[0])) +
                              u",)")
        tmp = u", ".join([space.unicode_w(space.repr(item))
                          for item in items])
        return space.wrap(u"(" + tmp + u")")

    def descr_hash(self, space):
        raise NotImplementedError

    def descr_eq(self, space, w_other):
        raise NotImplementedError

    def descr_ne(self, space, w_other):
        raise NotImplementedError

    def _make_tuple_comparison(name):
        import operator
        op = getattr(operator, name)

        def compare_tuples(self, space, w_other):
            if not isinstance(w_other, W_AbstractTupleObject):
                return space.w_NotImplemented
            return _compare_tuples(self, space, w_other)

        @jit.look_inside_iff(_unroll_condition_cmp)
        def _compare_tuples(self, space, w_other):
            items1 = self.tolist()
            items2 = w_other.tolist()
            ncmp = min(len(items1), len(items2))
            # Search for the first index where items are different
            for p in range(ncmp):
                if not space.eq_w(items1[p], items2[p]):
                    return getattr(space, name)(items1[p], items2[p])
            # No more items to compare -- compare sizes
            return space.newbool(op(len(items1), len(items2)))

        compare_tuples.__name__ = 'descr_' + name
        return compare_tuples

    descr_lt = _make_tuple_comparison('lt')
    descr_le = _make_tuple_comparison('le')
    descr_gt = _make_tuple_comparison('gt')
    descr_ge = _make_tuple_comparison('ge')

    @jit.look_inside_iff(lambda self, _1, _2: _unroll_condition(self))
    def descr_contains(self, space, w_obj):
        for w_item in self.tolist():
            if space.eq_w(w_item, w_obj):
                return space.w_True
        return space.w_False

    def descr_add(self, space, w_other):
        if not isinstance(w_other, W_AbstractTupleObject):
            return space.w_NotImplemented
        items1 = self.tolist()
        items2 = w_other.tolist()
        return space.newtuple(items1 + items2)

    def descr_mul(self, space, w_times):
        try:
            times = space.getindex_w(w_times, space.w_OverflowError)
        except OperationError, e:
            if e.match(space, space.w_TypeError):
                return space.w_NotImplemented
            raise
        if times == 1 and space.type(self) == space.w_tuple:
            return self
        items = self.tolist()
        return space.newtuple(items * times)

    def descr_getitem(self, space, w_index):
        if isinstance(w_index, W_SliceObject):
            return self._getslice(space, w_index)
        index = space.getindex_w(w_index, space.w_IndexError, "tuple index")
        return self.getitem(space, index)

    def _getslice(self, space, w_index):
        items = self.tolist()
        length = len(items)
        start, stop, step, slicelength = w_index.indices4(space, length)
        assert slicelength >= 0
        subitems = [None] * slicelength
        for i in range(slicelength):
            subitems[i] = items[start]
            start += step
        return space.newtuple(subitems)

    def descr_getnewargs(self, space):
        return space.newtuple([space.newtuple(self.tolist())])

    @jit.look_inside_iff(lambda self, _1, _2: _unroll_condition(self))
    def descr_count(self, space, w_obj):
        """count(obj) -> number of times obj appears in the tuple"""
        count = 0
        for w_item in self.tolist():
            if space.eq_w(w_item, w_obj):
                count += 1
        return space.wrap(count)

    @gateway.unwrap_spec(w_start=gateway.WrappedDefault(0),
                         w_stop=gateway.WrappedDefault(sys.maxint))
    @jit.look_inside_iff(lambda self, _1, _2, _3, _4: _unroll_condition(self))
    def descr_index(self, space, w_obj, w_start, w_stop):
        """index(obj, [start, [stop]]) -> first index that obj appears in the
        tuple
        """
        length = self.length()
        start, stop = slicetype.unwrap_start_stop(space, length, w_start,
                                                  w_stop)
        for i in range(start, min(stop, length)):
            w_item = self.tolist()[i]
            if space.eq_w(w_item, w_obj):
                return space.wrap(i)
        raise OperationError(space.w_ValueError,
                             space.wrap("tuple.index(x): x not in tuple"))

W_AbstractTupleObject.typedef = StdTypeDef(
    "tuple",
    __doc__ = '''tuple() -> an empty tuple
tuple(sequence) -> tuple initialized from sequence's items

If the argument is a tuple, the return value is the same object.''',
    __new__ = interp2app(W_AbstractTupleObject.descr_new),
    __repr__ = interp2app(W_AbstractTupleObject.descr_repr),
    __hash__ = interpindirect2app(W_AbstractTupleObject.descr_hash),

    __eq__ = interpindirect2app(W_AbstractTupleObject.descr_eq),
    __ne__ = interpindirect2app(W_AbstractTupleObject.descr_ne),
    __lt__ = interp2app(W_AbstractTupleObject.descr_lt),
    __le__ = interp2app(W_AbstractTupleObject.descr_le),
    __gt__ = interp2app(W_AbstractTupleObject.descr_gt),
    __ge__ = interp2app(W_AbstractTupleObject.descr_ge),

    __len__ = interp2app(W_AbstractTupleObject.descr_len),
    __iter__ = interp2app(W_AbstractTupleObject.descr_iter),
    __contains__ = interp2app(W_AbstractTupleObject.descr_contains),

    __add__ = interp2app(W_AbstractTupleObject.descr_add),
    __mul__ = interp2app(W_AbstractTupleObject.descr_mul),
    __rmul__ = interp2app(W_AbstractTupleObject.descr_mul),

    __getitem__ = interp2app(W_AbstractTupleObject.descr_getitem),

    __getnewargs__ = interp2app(W_AbstractTupleObject.descr_getnewargs),
    count = interp2app(W_AbstractTupleObject.descr_count),
    index = interp2app(W_AbstractTupleObject.descr_index)
)


class W_TupleObject(W_AbstractTupleObject):
    _immutable_fields_ = ['wrappeditems[*]']

    def __init__(self, wrappeditems):
        make_sure_not_resized(wrappeditems)
        self.wrappeditems = wrappeditems

    def tolist(self):
        return self.wrappeditems

    def getitems_copy(self):
        return self.wrappeditems[:]  # returns a resizable list

    def length(self):
        return len(self.wrappeditems)

    @jit.look_inside_iff(lambda self, _1: _unroll_condition(self))
    def descr_hash(self, space):
        mult = 1000003
        x = 0x345678
        z = len(self.wrappeditems)
        for w_item in self.wrappeditems:
            y = space.hash_w(w_item)
            x = (x ^ y) * mult
            z -= 1
            mult += 82520 + z + z
        x += 97531
        return space.wrap(intmask(x))

    def descr_eq(self, space, w_other):
        if not isinstance(w_other, W_AbstractTupleObject):
            return space.w_NotImplemented
        return self._descr_eq(space, w_other)

    @jit.look_inside_iff(_unroll_condition_cmp)
    def _descr_eq(self, space, w_other):
        items1 = self.wrappeditems
        items2 = w_other.tolist()
        lgt1 = len(items1)
        lgt2 = len(items2)
        if lgt1 != lgt2:
            return space.w_False
        for i in range(lgt1):
            item1 = items1[i]
            item2 = items2[i]
            if not space.eq_w(item1, item2):
                return space.w_False
        return space.w_True

    descr_ne = negate(descr_eq)

    def getitem(self, space, index):
        try:
            return self.wrappeditems[index]
        except IndexError:
            raise OperationError(space.w_IndexError,
                                 space.wrap("tuple index out of range"))


def wraptuple(space, list_w):
    if space.config.objspace.std.withspecialisedtuple:
        from specialisedtupleobject import makespecialisedtuple, NotSpecialised
        try:
            return makespecialisedtuple(space, list_w)
        except NotSpecialised:
            pass
    return W_TupleObject(list_w)
