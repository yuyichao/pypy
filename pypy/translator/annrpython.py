from __future__ import generators

from pypy.translator.annheap import AnnotationHeap, Transaction
from pypy.translator.annotation import XCell, XConstant, Annotation
from pypy.objspace.flow.model import Variable, Constant, SpaceOperation


class RPythonAnnotator:
    """Block annotator for RPython.
    See description in doc/transation/annotation.txt."""

    def __init__(self):
        self.heap = AnnotationHeap()
        self.pendingblocks = []  # list of (block, list-of-XCells)
        self.bindings = {}       # map Variables/Constants to XCells/XConstants
        self.annotated = {}      # set of blocks already seen


    #___ convenience high-level interface __________________

    def build_types(self, flowgraph, input_arg_types):
        """Recursively build annotations about the specific entry point."""
        # make input arguments and set their type
        inputcells = [XCell() for arg in flowgraph.getargs()]
        t = self.transaction()
        for cell, arg_type in zip(inputcells, input_arg_types):
            t.set_type(cell, arg_type)
        # register the entry point
        self.addpendingblock(flowgraph.startblock, inputcells)
        # recursively proceed until no more pending block is left
        self.complete()


    #___ medium-level interface ____________________________

    def addpendingblock(self, block, cells):
        """Register an entry point into block with the given input cells."""
        self.pendingblocks.append((block, cells))

    def transaction(self):
        """Start a Transaction.  Each new Annotation is marked as depending
        on the Annotations queried for during the same Transation."""
        return Transaction(self.heap)

    def complete(self):
        """Process pending blocks until none is left."""
        while self.pendingblocks:
            # XXX don't know if it is better to pop from the head or the tail.
            # let's do it breadth-first and pop from the head (oldest first).
            # that's more stacklessy.
            block, cells = self.pendingblocks.pop(0)
            self.processblock(block, cells)

    def binding(self, arg):
        "XCell or XConstant corresponding to the given Variable or Constant."
        try:
            return self.bindings[arg]
        except KeyError:
            if not isinstance(arg, Constant):
                raise   # propagate missing bindings for Variables
            result = XConstant(arg.value)
            self.consider_const(result, arg)
            self.bindings[arg] = result
            return result

    def bindnew(self, arg):
        "Force the creation of a new binding for the given Variable."
        assert isinstance(arg, Variable)
        self.bindings[arg] = result = XCell()
        return result

    def constant(self, value):
        "Turn a value into an XConstant with the proper annotations."
        return self.binding(Constant(value))


    #___ simplification (should be moved elsewhere?) _______

    def reverse_binding(self, known_variables, cell):
        """This is a hack."""
        # In simplify_calls, when we are trying to create the new
        # SpaceOperation, all we have are XCells.  But SpaceOperations take
        # Variables, not XCells.  Trouble is, we don't always have a Variable
        # that just happens to be bound to the given XCells.  A typical
        # example would be if the tuple of arguments was created from another
        # basic block or even another function.  Well I guess there is no
        # clean solution.
        if isinstance(cell, XConstant):
            return Constant(cell.value)
        else:
            for v in known_variables:
                if self.bindings[v] == cell:
                    return v
            else:
                raise CannotSimplify

    def simplify_calls(self):
        t = self.transaction()
        for block in self.annotated:
            known_variables = block.inputargs[:]
            newops = []
            for op in block.operations:
                try:
                    if op.opname == "call":
                        func, varargs, kwargs = [self.binding(a)
                                                 for a in op.args]
                        c = t.get('len', [varargs])
                        if not isinstance(c, XConstant):
                            raise CannotSimplify
                        length = c.value
                        v = self.reverse_binding(known_variables, func)
                        args = [v]
                        for i in range(length):
                            c = t.get('getitem', [varargs, self.constant(i)])
                            if c is None:
                                raise CannotSimplify
                            v = self.reverse_binding(known_variables, c)
                            args.append(v)
                        op = SpaceOperation('simple_call', args, op.result)
                        # XXX check that kwargs is empty
                except CannotSimplify:
                    pass
                newops.append(op)
                known_variables.append(op.result)
            block.operations = newops

    def simplify(self):
        self.simplify_calls()


    #___ flowing annotations in blocks _____________________

    def processblock(self, block, cells):
        if block not in self.annotated:
            self.annotated[block] = True
            self.flowin(block, cells)
        else:
            # already seen; merge each of the block's input variable
            newcells = []
            reflow = False
            for a, cell2 in zip(block.inputargs, cells):
                cell1 = self.bindings[a]   # old binding
                newcell = self.heap.merge(cell1, cell2)
                newcells.append(newcell)
                reflow = reflow or (newcell != cell1 and newcell != cell2)
            # no need to re-flowin unless there is a completely new cell
            if reflow:
                self.flowin(block, newcells)

    def flowin(self, block, inputcells):
        for a, cell in zip(block.inputargs, inputcells):
            self.bindings[a] = cell
        for op in block.operations:
            self.consider_op(op)
        for link in block.exits:
            cells = [self.binding(a) for a in link.args]
            self.addpendingblock(link.target, cells)


    #___ creating the annotations based on operations ______

    def consider_op(self,op):
        argcells = [self.binding(a) for a in op.args]
        resultcell = self.bindnew(op.result)
        consider_meth = getattr(self,'consider_op_'+op.opname,None)
        if consider_meth is not None:
            consider_meth(argcells, resultcell, self.transaction())

    def consider_op_add(self, (arg1,arg2), result, t):
        type1 = t.get_type(arg1)
        type2 = t.get_type(arg2)
        if type1 is int and type2 is int:
            t.set_type(result, int)
        elif type1 in (int, long) and type2 in (int, long):
            t.set_type(result, long)
        if type1 is str and type2 is str:
            t.set_type(result, str)
        if type1 is list and type2 is list:
            t.set_type(result, list)

    consider_op_inplace_add = consider_op_add

    def consider_op_sub(self, (arg1,arg2), result, t):
        type1 = t.get_type(arg1)
        type2 = t.get_type(arg2)
        if type1 is int and type2 is int:
            t.set_type(result, int)
        elif type1 in (int, long) and type2 in (int, long):
            t.set_type(result, long)

    consider_op_and_ = consider_op_sub # trailing underline
    consider_op_inplace_lshift = consider_op_sub

    def consider_op_is_true(self, (arg,), result, t):
        t.set_type(result, bool)

    consider_op_not_ = consider_op_is_true

    def consider_op_lt(self, (arg1,arg2), result, t):
        t.set_type(result, bool)

    consider_op_le = consider_op_lt
    consider_op_eq = consider_op_lt
    consider_op_ne = consider_op_lt
    consider_op_gt = consider_op_lt
    consider_op_ge = consider_op_lt

    def consider_op_newtuple(self, args, result, t):
        t.set_type(result,tuple)
        t.set("len", [result], self.constant(len(args)))
        for i in range(len(args)):
            t.set("getitem", [result, self.constant(i)], args[i])

    def consider_op_newlist(self, args, result, t):
        t.set_type(result, list)

    def consider_op_newslice(self, args, result, t):
        t.set_type(result, slice)

    def consider_op_getitem(self, (arg1,arg2), result, t):
        type1 = t.get_type(arg1)
        type2 = t.get_type(arg2)
        if type1 in (list, tuple) and type2 is slice:
            t.set_type(result, type1)

    def consider_op_call(self, (func,varargs,kwargs), result, t):
        if not isinstance(func, XConstant):
            return
        func = func.value
        # XXX: generalize this later
        if func is range:
            t.set_type(result, list)
        if func is pow:
            tp1 = t.get_type(t.get('getitem', [varargs, self.constant(0)]))
            tp2 = t.get_type(t.get('getitem', [varargs, self.constant(1)]))
            if tp1 is int and tp2 is int:
                t.set_type(result, int)

    def consider_const(self,to_var,const):
        t = self.transaction()
        t.set('immutable', [], to_var)
        if getattr(const, 'dummy', False):
            return   # undefined local variables
        t.set_type(to_var,type(const.value))
        if isinstance(const.value, list):
            pass # XXX say something about the type of the elements
        elif isinstance(const.value, tuple):
            pass # XXX say something about the elements


class CannotSimplify(Exception):
    pass
