"""Implements the core parts of flow graph creation, in tandem
with rpython.flowspace.flowcontext.
"""

import __builtin__
import sys
import types
from inspect import CO_NEWLOCALS

from rpython.flowspace.argument import CallSpec
from rpython.flowspace.model import (Constant, Variable, WrapException,
    UnwrapException, checkgraph, const)
from rpython.flowspace.bytecode import HostCode
from rpython.flowspace.operation import op
from rpython.flowspace.flowcontext import (FlowSpaceFrame, fixeggblocks,
    FSException, FlowingError)
from rpython.flowspace.generator import (tweak_generator_graph,
        bootstrap_generator)
from rpython.flowspace.pygraph import PyGraph
from rpython.flowspace.specialcase import SPECIAL_CASES
from rpython.rlib.unroll import unrolling_iterable, _unroller
from rpython.rlib import rstackovf


# the following gives us easy access to declare more for applications:
NOT_REALLY_CONST = {
    Constant(sys): {
        Constant('maxint'): True,
        Constant('maxunicode'): True,
        Constant('api_version'): True,
        Constant('exit'): True,
        Constant('exc_info'): True,
        Constant('getrefcount'): True,
        Constant('getdefaultencoding'): True,
        # this is an incomplete list of true constants.
        # if we add much more, a dedicated class
        # might be considered for special objects.
        }
    }

# built-ins that can always raise exceptions
builtins_exceptions = {
    int: [ValueError],
    float: [ValueError],
    chr: [ValueError],
    unichr: [ValueError],
    unicode: [UnicodeDecodeError],
}


def _assert_rpythonic(func):
    """Raise ValueError if ``func`` is obviously not RPython"""
    if func.func_doc and func.func_doc.lstrip().startswith('NOT_RPYTHON'):
        raise ValueError("%r is tagged as NOT_RPYTHON" % (func,))
    if func.func_code.co_cellvars:
        raise ValueError("RPython functions cannot create closures")
    if not (func.func_code.co_flags & CO_NEWLOCALS):
        raise ValueError("The code object for a RPython function should have "
                "the flag CO_NEWLOCALS set.")


# ______________________________________________________________________
class FlowObjSpace(object):
    """NOT_RPYTHON.
    The flow objspace space is used to produce a flow graph by recording
    the space operations that the interpreter generates when it interprets
    (the bytecode of) some function.
    """
    w_None = Constant(None)
    sys = Constant(sys)
    w_False = Constant(False)
    w_True = Constant(True)
    w_type = Constant(type)
    w_tuple = Constant(tuple)
    for exc in [KeyError, ValueError, IndexError, StopIteration,
                AssertionError, TypeError, AttributeError, ImportError]:
        clsname = exc.__name__
        locals()['w_' + clsname] = Constant(exc)

    # the following exceptions should not show up
    # during flow graph construction
    w_NameError = 'NameError'
    w_UnboundLocalError = 'UnboundLocalError'

    specialcases = SPECIAL_CASES
    # objects which should keep their SomeObjectness
    not_really_const = NOT_REALLY_CONST

    def build_flow(self, func):
        return build_flow(func, self)

    is_ = None     # real version added by add_operations()
    id  = None     # real version added by add_operations()

    def newdict(self, module="ignored"):
        return self.frame.do_operation('newdict')

    def newtuple(self, args_w):
        if all(isinstance(w_arg, Constant) for w_arg in args_w):
            content = [w_arg.value for w_arg in args_w]
            return Constant(tuple(content))
        else:
            return self.frame.do_operation('newtuple', *args_w)

    def newlist(self, args_w, sizehint=None):
        return self.frame.do_operation('newlist', *args_w)

    def newslice(self, w_start, w_stop, w_step):
        return self.frame.do_operation('newslice', w_start, w_stop, w_step)

    def newbool(self, b):
        if b:
            return self.w_True
        else:
            return self.w_False

    def newfunction(self, w_code, w_globals, defaults_w):
        if not all(isinstance(value, Constant) for value in defaults_w):
            raise FlowingError("Dynamically created function must"
                    " have constant default values.")
        code = w_code.value
        globals = w_globals.value
        defaults = tuple([default.value for default in defaults_w])
        fn = types.FunctionType(code, globals, code.co_name, defaults)
        return Constant(fn)

    def exc_wrap(self, exc):
        w_value = const(exc)
        w_type = const(type(exc))
        return FSException(w_type, w_value)

    def exception_match(self, w_exc_type, w_check_class):
        """Checks if the given exception type matches 'w_check_class'."""
        frame = self.frame
        if not isinstance(w_check_class, Constant):
            raise FlowingError("Non-constant except guard.")
        check_class = w_check_class.value
        if check_class in (NotImplementedError, AssertionError):
            raise FlowingError(
                "Catching %s is not valid in RPython" % check_class.__name__)
        if not isinstance(check_class, tuple):
            # the simple case
            return frame.guessbool(self.issubtype(w_exc_type, w_check_class))
        # special case for StackOverflow (see rlib/rstackovf.py)
        if check_class == rstackovf.StackOverflow:
            w_real_class = const(rstackovf._StackOverflow)
            return frame.guessbool(self.issubtype(w_exc_type, w_real_class))
        # checking a tuple of classes
        for w_klass in self.unpackiterable(w_check_class):
            if self.exception_match(w_exc_type, w_klass):
                return True
        return False

    def exc_from_raise(self, w_arg1, w_arg2):
        """
        Create a wrapped exception from the arguments of a raise statement.

        Returns an FSException object whose w_value is an instance of w_type.
        """
        frame = self.frame
        if frame.guessbool(self.call_function(const(isinstance), w_arg1,
                self.w_type)):
            # this is for all cases of the form (Class, something)
            if frame.guessbool(self.is_(w_arg2, self.w_None)):
                # raise Type: we assume we have to instantiate Type
                w_value = self.call_function(w_arg1)
            else:
                w_valuetype = self.type(w_arg2)
                if frame.guessbool(self.issubtype(w_valuetype, w_arg1)):
                    # raise Type, Instance: let etype be the exact type of value
                    w_value = w_arg2
                else:
                    # raise Type, X: assume X is the constructor argument
                    w_value = self.call_function(w_arg1, w_arg2)
        else:
            # the only case left here is (inst, None), from a 'raise inst'.
            if not frame.guessbool(self.is_(w_arg2, self.w_None)):
                raise self.exc_wrap(TypeError(
                    "instance exception may not have a separate value"))
            w_value = w_arg1
        w_type = self.type(w_value)
        return FSException(w_type, w_value)

    def unpackiterable(self, w_iterable):
        if isinstance(w_iterable, Constant):
            l = w_iterable.value
            return [const(x) for x in l]
        else:
            raise UnwrapException("cannot unpack a Variable iterable ")

    def unpack_sequence(self, w_iterable, expected_length):
        if isinstance(w_iterable, Constant):
            l = list(w_iterable.value)
            if len(l) != expected_length:
                raise ValueError
            return [const(x) for x in l]
        else:
            w_len = self.len(w_iterable)
            w_correct = self.eq(w_len, const(expected_length))
            if not self.frame.guessbool(self.bool(w_correct)):
                e = self.exc_from_raise(self.w_ValueError, self.w_None)
                raise e
            return [self.frame.do_operation('getitem', w_iterable, const(i))
                        for i in range(expected_length)]

    # ____________________________________________________________
    def not_(self, w_obj):
        return const(not self.frame.guessbool(self.bool(w_obj)))

    def iter(self, w_iterable):
        if isinstance(w_iterable, Constant):
            iterable = w_iterable.value
            if isinstance(iterable, unrolling_iterable):
                return const(iterable.get_unroller())
        w_iter = self.frame.do_operation("iter", w_iterable)
        return w_iter

    def next(self, w_iter):
        frame = self.frame
        if isinstance(w_iter, Constant):
            it = w_iter.value
            if isinstance(it, _unroller):
                try:
                    v, next_unroller = it.step()
                except IndexError:
                    raise self.exc_wrap(StopIteration())
                else:
                    frame.replace_in_stack(it, next_unroller)
                    return const(v)
        w_item = frame.do_operation("next", w_iter)
        frame.guessexception([StopIteration, RuntimeError], force=True)
        return w_item


    def getattr(self, w_obj, w_name):
        # handling special things like sys
        # unfortunately this will never vanish with a unique import logic :-(
        if w_obj in self.not_really_const:
            const_w = self.not_really_const[w_obj]
            if w_name not in const_w:
                return self.frame.do_op(op.getattr(w_obj, w_name))
        if w_obj.foldable() and w_name.foldable():
            obj, name = w_obj.value, w_name.value
            try:
                result = getattr(obj, name)
            except Exception, e:
                etype = e.__class__
                msg = "getattr(%s, %s) always raises %s: %s" % (
                    obj, name, etype, e)
                raise FlowingError(msg)
            try:
                return const(result)
            except WrapException:
                pass
        return self.frame.do_op(op.getattr(w_obj, w_name))

    def import_name(self, name, glob=None, loc=None, frm=None, level=-1):
        try:
            mod = __import__(name, glob, loc, frm, level)
        except ImportError as e:
            raise self.exc_wrap(e)
        return const(mod)

    def import_from(self, w_module, w_name):
        assert isinstance(w_module, Constant)
        assert isinstance(w_name, Constant)
        # handle sys
        if w_module in self.not_really_const:
            const_w = self.not_really_const[w_module]
            if w_name not in const_w:
                return self.frame.do_op(op.getattr(w_module, w_name))
        try:
            return const(getattr(w_module.value, w_name.value))
        except AttributeError:
            raise self.exc_wrap(ImportError(
                "cannot import name '%s'" % w_name.value))

    def call_method(self, w_obj, methname, *arg_w):
        w_meth = self.getattr(w_obj, const(methname))
        return self.call_function(w_meth, *arg_w)

    def call_function(self, w_func, *args_w):
        args = CallSpec(list(args_w))
        return self.call_args(w_func, args)

    def appcall(self, func, *args_w):
        """Call an app-level RPython function directly"""
        w_func = const(func)
        return self.frame.do_operation('simple_call', w_func, *args_w)

    def call_args(self, w_callable, args):
        if isinstance(w_callable, Constant):
            fn = w_callable.value
            if hasattr(fn, "_flowspace_rewrite_directly_as_"):
                fn = fn._flowspace_rewrite_directly_as_
                w_callable = const(fn)
            try:
                sc = self.specialcases[fn]   # TypeError if 'fn' not hashable
            except (KeyError, TypeError):
                pass
            else:
                assert args.keywords == {}, "should not call %r with keyword arguments" % (fn,)
                if args.w_stararg is not None:
                    args_w = args.arguments_w + self.unpackiterable(args.w_stararg)
                else:
                    args_w = args.arguments_w
                return sc(self, *args_w)

        if args.keywords or isinstance(args.w_stararg, Variable):
            shape, args_w = args.flatten()
            w_res = self.frame.do_operation('call_args', w_callable,
                    Constant(shape), *args_w)
        else:
            if args.w_stararg is not None:
                args_w = args.arguments_w + self.unpackiterable(args.w_stararg)
            else:
                args_w = args.arguments_w
            w_res = self.frame.do_operation('simple_call', w_callable, *args_w)

        if isinstance(w_callable, Constant):
            c = w_callable.value
            if (isinstance(c, (types.BuiltinFunctionType,
                               types.BuiltinMethodType,
                               types.ClassType,
                               types.TypeType)) and
                  c.__module__ in ['__builtin__', 'exceptions']):
                if c in builtins_exceptions:
                    self.frame.guessexception(builtins_exceptions[c])
                return w_res
        # *any* exception for non-builtins
        self.frame.guessexception([Exception])
        return w_res

    def find_global(self, w_globals, varname):
        try:
            value = w_globals.value[varname]
        except KeyError:
            # not in the globals, now look in the built-ins
            try:
                value = getattr(__builtin__, varname)
            except AttributeError:
                raise FlowingError("global name '%s' is not defined" % varname)
        return const(value)

def make_op(cls):
    def generic_operator(self, *args):
        return cls(*args).eval(self.frame)
    return generic_operator

for cls in op.__dict__.values():
    if getattr(FlowObjSpace, cls.opname, None) is None:
        setattr(FlowObjSpace, cls.opname, make_op(cls))


def build_flow(func, space=FlowObjSpace()):
    """
    Create the flow graph for the function.
    """
    _assert_rpythonic(func)
    code = HostCode._from_code(func.func_code)
    if (code.is_generator and
            not hasattr(func, '_generator_next_method_of_')):
        graph = PyGraph(func, code)
        block = graph.startblock
        for name, w_value in zip(code.co_varnames, block.framestate.mergeable):
            if isinstance(w_value, Variable):
                w_value.rename(name)
        return bootstrap_generator(graph)
    graph = PyGraph(func, code)
    frame = space.frame = FlowSpaceFrame(space, graph, code)
    frame.build_flow()
    fixeggblocks(graph)
    checkgraph(graph)
    if code.is_generator:
        tweak_generator_graph(graph)
    return graph
