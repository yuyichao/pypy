from pypy.jit.metainterp.optimizeopt.optimizer import Optimization
from pypy.jit.metainterp.resoperation import rop, ResOperation
from pypy.jit.metainterp.optimizeopt.util import (make_dispatcher_method,
    args_dict)

class OptPure(Optimization):
    def __init__(self):
        self.posponedop = None
        self.pure_operations = args_dict()

    def propagate_forward(self, op):
        dispatch_opt(self, op)

    def optimize_default(self, op):
        canfold = op.is_always_pure()
        if op.is_ovf():
            self.posponedop = op
            return
        if self.posponedop:
            nextop = op
            op = self.posponedop
            self.posponedop = None
            canfold = nextop.getopnum() == rop.GUARD_NO_OVERFLOW
        else:
            nextop = None

        if canfold:
            for i in range(op.numargs()):
                if self.get_constant_box(op.getarg(i)) is None:
                    break
            else:
                # all constant arguments: constant-fold away
                resbox = self.optimizer.constant_fold(op)
                # note that INT_xxx_OVF is not done from here, and the
                # overflows in the INT_xxx operations are ignored
                self.optimizer.make_constant(op.result, resbox)
                return

            # did we do the exact same operation already?
            args = self.optimizer.make_args_key(op)
            oldop = self.pure_operations.get(args, None)
            if oldop is not None and oldop.getdescr() is op.getdescr():
                assert oldop.getopnum() == op.getopnum()
                self.optimizer.make_equal_to(op.result, self.getvalue(oldop.result),
                                   True)
                return
            else:
                self.pure_operations[args] = op
                self.optimizer.remember_emitting_pure(op)

        # otherwise, the operation remains
        self.emit_operation(op)
        if op.returns_bool_result():
            self.optimizer.bool_boxes[self.getvalue(op.result)] = None        
        if nextop:
            self.emit_operation(nextop)

    def optimize_CALL_PURE(self, op):
        arg_consts = []
        for i in range(op.numargs()):
            arg = op.getarg(i)
            const = self.get_constant_box(arg)
            if const is None:
                break
            arg_consts.append(const)
        else:
            # all constant arguments: check if we already know the result
            try:
                result = self.optimizer.call_pure_results[arg_consts]
            except KeyError:
                pass
            else:
                self.make_constant(op.result, result)
                return

        args = self.optimizer.make_args_key(op)
        oldop = self.pure_operations.get(args, None)
        if oldop is not None and oldop.getdescr() is op.getdescr():
            assert oldop.getopnum() == op.getopnum()
            self.make_equal_to(op.result, self.getvalue(oldop.result))
            return
        else:
            self.pure_operations[args] = op
            self.optimizer.remember_emitting_pure(op)

        # replace CALL_PURE with just CALL
        args = op.getarglist()
        self.emit_operation(ResOperation(rop.CALL, args, op.result,
                                         op.getdescr()))

    def flush(self):
        assert self.posponedop is None

    def new(self):
        assert self.posponedop is None
        return OptPure()

    def setup(self):
        self.optimizer.optpure = self

    def pure(self, opnum, args, result):
        op = ResOperation(opnum, args, result)
        key = self.optimizer.make_args_key(op)
        if key not in self.pure_operations:
            self.pure_operations[key] = op

    def has_pure_result(self, opnum, args, descr):
        op = ResOperation(opnum, args, None, descr)
        key = self.optimizer.make_args_key(op)
        op = self.pure_operations.get(key, None)
        if op is None:
            return False
        return op.getdescr() is descr

    def get_pure_result(self, key):
        return self.pure_operations.get(key, None)

dispatch_opt = make_dispatcher_method(OptPure, 'optimize_',
                                      default=OptPure.optimize_default)
