""" transparent list implementation
"""
from pypy.interpreter.error import OperationError
from pypy.interpreter import baseobjspace


def transparent_class(name, BaseCls):
    class W_Transparent(BaseCls):
        ignore_for_isinstance_cache = True

        def __init__(self, space, w_type, w_controller):
            self.w_type = w_type
            self.w_controller = w_controller

        def descr_call_mismatch(self, space, name, reqcls, args):
            args_w = args.arguments_w[:]
            args_w[0] = space.wrap(name)
            args = args.replace_arguments(args_w)
            return space.call_args(self.w_controller, args)

        def getclass(self, space):
            return self.w_type

        def setclass(self, space, w_subtype):
            raise OperationError(space.w_TypeError,
                                 space.wrap("You cannot override __class__ for transparent proxies"))

        def getdictvalue(self, space, attr):
            try:
                return space.call_function(self.w_controller, space.wrap('__getattribute__'),
                   space.wrap(attr))
            except OperationError, e:
                if not e.match(space, space.w_AttributeError):
                    raise
                return None

        def setdictvalue(self, space, attr, w_value):
            try:
                space.call_function(self.w_controller, space.wrap('__setattr__'),
                   space.wrap(attr), w_value)
                return True
            except OperationError, e:
                if not e.match(space, space.w_AttributeError):
                    raise
                return False

        def deldictvalue(self, space, attr):
            try:
                space.call_function(self.w_controller, space.wrap('__delattr__'),
                   space.wrap(attr))
                return True
            except OperationError, e:
                if not e.match(space, space.w_AttributeError):
                    raise
                return False

        def getdict(self, space):
            return self.getdictvalue(space, '__dict__')

        def setdict(self, space, w_dict):
            if not self.setdictvalue(space, '__dict__', w_dict):
                baseobjspace.W_Root.setdict(self, space, w_dict)

    W_Transparent.__name__ = name
    return W_Transparent

W_Transparent = transparent_class('W_Transparent', baseobjspace.W_Root)
