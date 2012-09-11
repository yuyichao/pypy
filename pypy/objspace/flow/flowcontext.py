import collections
import sys
from pypy.tool.error import FlowingError
from pypy.interpreter.executioncontext import ExecutionContext
from pypy.interpreter.error import OperationError
from pypy.interpreter.pytraceback import PyTraceback
from pypy.interpreter import pyframe
from pypy.interpreter.nestedscope import Cell
from pypy.interpreter.pycode import CO_OPTIMIZED, CO_NEWLOCALS
from pypy.interpreter.argument import ArgumentsForTranslation
from pypy.interpreter.pyopcode import (Return, Yield, SuspendedUnroller,
        SReturnValue, SApplicationException, BytecodeCorruption, Reraise,
        RaiseWithExplicitTraceback)
from pypy.objspace.flow.operation import (ImplicitOperationError,
        OperationThatShouldNotBePropagatedError)
from pypy.objspace.flow.model import *
from pypy.objspace.flow.framestate import (FrameState, recursively_unflatten,
        recursively_flatten)
from pypy.objspace.flow.bytecode import HostCode

class StopFlowing(Exception):
    pass

class MergeBlock(Exception):
    def __init__(self, block, currentstate):
        self.block = block
        self.currentstate = currentstate

class SpamBlock(Block):
    # make slots optional, for debugging
    if hasattr(Block, '__slots__'):
        __slots__ = "dead framestate".split()

    def __init__(self, framestate):
        Block.__init__(self, framestate.getvariables())
        self.framestate = framestate
        self.dead = False

class EggBlock(Block):
    # make slots optional, for debugging
    if hasattr(Block, '__slots__'):
        __slots__ = "prevblock booloutcome last_exception".split()

    def __init__(self, inputargs, prevblock, booloutcome):
        Block.__init__(self, inputargs)
        self.prevblock = prevblock
        self.booloutcome = booloutcome

    def extravars(self, last_exception=None, last_exc_value=None):
        self.last_exception = last_exception

# ____________________________________________________________

class Recorder:

    def append(self, operation):
        raise NotImplementedError

    def bytecode_trace(self, frame):
        pass

    def guessbool(self, ec, w_condition, **kwds):
        raise AssertionError, "cannot guessbool(%s)" % (w_condition,)


class BlockRecorder(Recorder):
    # Records all generated operations into a block.

    def __init__(self, block):
        self.crnt_block = block
        # saved state at the join point most recently seen
        self.last_join_point = None
        self.enterspamblock = isinstance(block, SpamBlock)

    def append(self, operation):
        if self.last_join_point is not None:
            # only add operations corresponding to the first bytecode
            raise MergeBlock(self.crnt_block, self.last_join_point)
        self.crnt_block.operations.append(operation)

    def bytecode_trace(self, frame):
        if self.enterspamblock:
            # If we have a SpamBlock, the first call to bytecode_trace()
            # occurs as soon as frame.resume() starts, before interpretation
            # really begins.
            varnames = frame.pycode.getvarnames()
            for name, w_value in zip(varnames, frame.getfastscope()):
                if isinstance(w_value, Variable):
                    w_value.rename(name)
            self.enterspamblock = False
        else:
            # At this point, we progress to the next bytecode.  When this
            # occurs, we no longer allow any more operations to be recorded in
            # the same block.  We will continue, to figure out where the next
            # such operation *would* appear, and we make a join point just
            # before.
            self.last_join_point = frame.getstate()

    def guessbool(self, ec, w_condition, cases=[False,True],
                  replace_last_variable_except_in_first_case = None):
        block = self.crnt_block
        bvars = vars = vars2 = block.getvariables()
        links = []
        first = True
        attach = {}
        for case in cases:
            if first:
                first = False
            elif replace_last_variable_except_in_first_case is not None:
                assert block.operations[-1].result is bvars[-1]
                vars = bvars[:-1]
                vars2 = bvars[:-1]
                for name, newvar in replace_last_variable_except_in_first_case(case):
                    attach[name] = newvar
                    vars.append(newvar)
                    vars2.append(Variable())
            egg = EggBlock(vars2, block, case)
            ec.pendingblocks.append(egg)
            link = ec.make_link(vars, egg, case)
            if attach:
                link.extravars(**attach)
                egg.extravars(**attach) # xxx
            links.append(link)

        block.exitswitch = w_condition
        block.closeblock(*links)
        # forked the graph. Note that False comes before True by default
        # in the exits tuple so that (just in case we need it) we
        # actually have block.exits[False] = elseLink and
        # block.exits[True] = ifLink.
        raise StopFlowing


class Replayer(Recorder):

    def __init__(self, block, booloutcome, nextreplayer):
        self.crnt_block = block
        self.listtoreplay = block.operations
        self.booloutcome = booloutcome
        self.nextreplayer = nextreplayer
        self.index = 0

    def append(self, operation):
        operation.result = self.listtoreplay[self.index].result
        assert operation == self.listtoreplay[self.index], (
            '\n'.join(["Not generating the same operation sequence:"] +
                      [str(s) for s in self.listtoreplay[:self.index]] +
                      ["  ---> | while repeating we see here"] +
                      ["       | %s" % operation] +
                      [str(s) for s in self.listtoreplay[self.index:]]))
        self.index += 1

    def guessbool(self, ec, w_condition, **kwds):
        assert self.index == len(self.listtoreplay)
        ec.recorder = self.nextreplayer
        return self.booloutcome

# ____________________________________________________________


class FlowExecutionContext(ExecutionContext):

    make_link = Link # overridable for transition tracking

    # disable superclass method
    bytecode_trace = None

    def guessbool(self, w_condition, **kwds):
        return self.recorder.guessbool(self, w_condition, **kwds)

    def guessexception(self, *classes):
        def replace_exc_values(case):
            if case is not Exception:
                yield 'last_exception', Constant(case)
                yield 'last_exc_value', Variable('last_exc_value')
            else:
                yield 'last_exception', Variable('last_exception')
                yield 'last_exc_value', Variable('last_exc_value')
        outcome = self.guessbool(c_last_exception,
                                 cases = [None] + list(classes),
                                 replace_last_variable_except_in_first_case = replace_exc_values)
        if outcome is None:
            w_exc_cls, w_exc_value = None, None
        else:
            egg = self.recorder.crnt_block
            w_exc_cls, w_exc_value = egg.inputargs[-2:]
            if isinstance(egg.last_exception, Constant):
                w_exc_cls = egg.last_exception
        return outcome, w_exc_cls, w_exc_value

    def build_flow(self, func, constargs={}):
        space = self.space
        self.frame = frame = FlowSpaceFrame(self.space, func, constargs)
        self.joinpoints = {}
        self.graph = frame._init_graph(func)
        self.pendingblocks = collections.deque([self.graph.startblock])

        while self.pendingblocks:
            block = self.pendingblocks.popleft()
            try:
                self.recorder = frame.recording(block)
                frame.frame_finished_execution = False
                next_instr = frame.last_instr
                while True:
                    next_instr = frame.handle_bytecode(next_instr)

            except ImplicitOperationError, e:
                if isinstance(e.w_type, Constant):
                    exc_cls = e.w_type.value
                else:
                    exc_cls = Exception
                msg = "implicit %s shouldn't occur" % exc_cls.__name__
                w_type = Constant(AssertionError)
                w_value = Constant(AssertionError(msg))
                link = self.make_link([w_type, w_value], self.graph.exceptblock)
                self.recorder.crnt_block.closeblock(link)

            except OperationError, e:
                if e.w_type is self.space.w_ImportError:
                    msg = 'import statement always raises %s' % e
                    raise ImportError(msg)
                w_value = e.get_w_value(self.space)
                link = self.make_link([e.w_type, w_value], self.graph.exceptblock)
                self.recorder.crnt_block.closeblock(link)

            except StopFlowing:
                pass

            except MergeBlock, e:
                self.mergeblock(e.block, e.currentstate)

            except Return:
                w_result = frame.popvalue()
                assert w_result is not None
                link = self.make_link([w_result], self.graph.returnblock)
                self.recorder.crnt_block.closeblock(link)

        del self.recorder
        self.fixeggblocks()


    def fixeggblocks(self):
        # EggBlocks reuse the variables of their previous block,
        # which is deemed not acceptable for simplicity of the operations
        # that will be performed later on the flow graph.
        for link in list(self.graph.iterlinks()):
                block = link.target
                if isinstance(block, EggBlock):
                    if (not block.operations and len(block.exits) == 1 and
                        link.args == block.inputargs):   # not renamed
                        # if the variables are not renamed across this link
                        # (common case for EggBlocks) then it's easy enough to
                        # get rid of the empty EggBlock.
                        link2 = block.exits[0]
                        link.args = list(link2.args)
                        link.target = link2.target
                        assert link2.exitcase is None
                    else:
                        mapping = {}
                        for a in block.inputargs:
                            mapping[a] = Variable(a)
                        block.renamevariables(mapping)
        for block in self.graph.iterblocks():
            if isinstance(link, SpamBlock):
                del link.framestate     # memory saver

    def mergeblock(self, currentblock, currentstate):
        next_instr = currentstate.next_instr
        # can 'currentstate' be merged with one of the blocks that
        # already exist for this bytecode position?
        candidates = self.joinpoints.setdefault(next_instr, [])
        for block in candidates:
            newstate = block.framestate.union(currentstate)
            if newstate is not None:
                # yes
                finished = newstate == block.framestate
                break
        else:
            # no
            newstate = currentstate.copy()
            finished = False
            block = None

        if finished:
            newblock = block
        else:
            newblock = SpamBlock(newstate)
        # unconditionally link the current block to the newblock
        outputargs = currentstate.getoutputargs(newstate)
        link = self.make_link(outputargs, newblock)
        currentblock.closeblock(link)
        # phew
        if not finished:
            if block is not None:
                # to simplify the graph, we patch the old block to point
                # directly at the new block which is its generalization
                block.dead = True
                block.operations = ()
                block.exitswitch = None
                outputargs = block.framestate.getoutputargs(newstate)
                block.recloseblock(self.make_link(outputargs, newblock))
                candidates.remove(block)
            candidates.insert(0, newblock)
            self.pendingblocks.append(newblock)

    def _convert_exc(self, operr):
        if isinstance(operr, ImplicitOperationError):
            # re-raising an implicit operation makes it an explicit one
            w_value = operr.get_w_value(self.space)
            operr = OperationError(operr.w_type, w_value)
        return operr

    # hack for unrolling iterables, don't use this
    def replace_in_stack(self, oldvalue, newvalue):
        w_new = Constant(newvalue)
        f = self.frame
        stack_items_w = f.locals_stack_w
        for i in range(f.valuestackdepth-1, f.pycode.co_nlocals-1, -1):
            w_v = stack_items_w[i]
            if isinstance(w_v, Constant):
                if w_v.value is oldvalue:
                    # replace the topmost item of the stack that is equal
                    # to 'oldvalue' with 'newvalue'.
                    stack_items_w[i] = w_new
                    break

class FlowSpaceFrame(pyframe.CPythonFrame):

    def __init__(self, space, func, constargs=None):
        code = HostCode._from_code(space, func.func_code)
        self.pycode = code
        self.space = space
        self.w_globals = Constant(func.func_globals)
        self.locals_stack_w = [None] * (code.co_nlocals + code.co_stacksize)
        self.valuestackdepth = code.co_nlocals
        self.lastblock = None

        if func.func_closure is not None:
            cl = [c.cell_contents for c in func.func_closure]
            closure = [Cell(Constant(value)) for value in cl]
        else:
            closure = []
        self.initialize_frame_scopes(closure, code)
        self.f_lineno = code.co_firstlineno
        self.last_instr = 0

        if constargs is None:
            constargs = {}
        formalargcount = code.getformalargcount()
        arg_list = [Variable() for i in range(formalargcount)]
        for position, value in constargs.items():
            arg_list[position] = Constant(value)
        self.setfastscope(arg_list)

        self.w_locals = None # XXX: only for compatibility with PyFrame

    def initialize_frame_scopes(self, closure, code):
        if not (code.co_flags & CO_NEWLOCALS):
            raise ValueError("The code object for a function should have "
                    "the flag CO_NEWLOCALS set.")
        if len(closure) != len(code.co_freevars):
            raise ValueError("code object received a closure with "
                                 "an unexpected number of free variables")
        self.cells = [Cell() for _ in code.co_cellvars] + closure

    def _init_graph(self, func):
        # CallableFactory.pycall may add class_ to functions that are methods
        name = func.func_name
        class_ = getattr(func, 'class_', None)
        if class_ is not None:
            name = '%s.%s' % (class_.__name__, name)
        for c in "<>&!":
            name = name.replace(c, '_')

        initialblock = SpamBlock(self.getstate())
        if self.pycode.is_generator:
            initialblock.operations.append(
                SpaceOperation('generator_mark', [], Variable()))
        graph = FunctionGraph(name, initialblock)
        graph.func = func
        # attach a signature and defaults to the graph
        # so that it becomes even more interchangeable with the function
        # itself
        graph.signature = self.pycode.signature()
        graph.defaults = func.func_defaults or ()
        graph.is_generator = self.pycode.is_generator
        return graph

    def getstate(self):
        # getfastscope() can return real None, for undefined locals
        data = self.save_locals_stack()
        if self.last_exception is None:
            data.append(Constant(None))
            data.append(Constant(None))
        else:
            data.append(self.last_exception.w_type)
            data.append(self.last_exception.get_w_value(self.space))
        recursively_flatten(self.space, data)
        nonmergeable = (self.get_blocklist(),
            self.last_instr)   # == next_instr when between bytecodes
        return FrameState(data, nonmergeable)

    def setstate(self, state):
        """ Reset the frame to the given state. """
        data = state.mergeable[:]
        recursively_unflatten(self.space, data)
        self.restore_locals_stack(data[:-2])  # Nones == undefined locals
        if data[-2] == Constant(None):
            assert data[-1] == Constant(None)
            self.last_exception = None
        else:
            self.last_exception = OperationError(data[-2], data[-1])
        blocklist, self.last_instr = state.nonmergeable
        self.set_blocklist(blocklist)

    def recording(self, block):
        """ Setup recording of the block and return the recorder. """
        parentblocks = []
        parent = block
        while isinstance(parent, EggBlock):
            parent = parent.prevblock
            parentblocks.append(parent)
        # parentblocks = [Egg, Egg, ..., Egg, Spam] not including block
        if parent.dead:
            raise StopFlowing
        self.setstate(parent.framestate)
        recorder = BlockRecorder(block)
        prevblock = block
        for parent in parentblocks:
            recorder = Replayer(parent, prevblock.booloutcome, recorder)
            prevblock = parent
        return recorder

    def handle_bytecode(self, next_instr):
        try:
            next_instr = self.dispatch_bytecode(next_instr)
        except OperationThatShouldNotBePropagatedError, e:
            raise Exception(
                'found an operation that always raises %s: %s' % (
                    self.space.unwrap(e.w_type).__name__,
                    self.space.unwrap(e.get_w_value(self.space))))
        except OperationError, operr:
            self.attach_traceback(operr)
            next_instr = self.handle_operation_error(operr)
        except Reraise:
            operr = self.last_exception
            next_instr = self.handle_operation_error(operr)
        except RaiseWithExplicitTraceback, e:
            next_instr = self.handle_operation_error(e.operr)
        return next_instr

    def attach_traceback(self, operr):
        if self.pycode.hidden_applevel:
            return
        tb = operr.get_traceback()
        tb = PyTraceback(self.space, self, self.last_instr, tb)
        operr.set_traceback(tb)

    def handle_operation_error(self, operr):
        block = self.unrollstack(SApplicationException.kind)
        if block is None:
            # no handler found for the OperationError
            # try to preserve the CPython-level traceback
            import sys
            tb = sys.exc_info()[2]
            raise OperationError, operr, tb
        else:
            unroller = SApplicationException(operr)
            next_instr = block.handle(self, unroller)
            return next_instr

    def enter_bytecode(self, next_instr):
        self.last_instr = next_instr
        self.space.executioncontext.recorder.bytecode_trace(self)

    def dispatch_bytecode(self, next_instr):
        while True:
            self.enter_bytecode(next_instr)
            next_instr, methodname, oparg = self.pycode.read(next_instr)
            res = getattr(self, methodname)(oparg, next_instr)
            if res is not None:
                next_instr = res

    def IMPORT_NAME(self, nameindex, next_instr):
        space = self.space
        modulename = self.getname_u(nameindex)
        glob = space.unwrap(self.w_globals)
        fromlist = space.unwrap(self.popvalue())
        level = self.popvalue().value
        w_obj = space.import_name(modulename, glob, None, fromlist, level)
        self.pushvalue(w_obj)

    def IMPORT_FROM(self, nameindex, next_instr):
        w_name = self.getname_w(nameindex)
        w_module = self.peekvalue()
        self.pushvalue(self.space.import_from(w_module, w_name))

    def RETURN_VALUE(self, oparg, next_instr):
        w_returnvalue = self.popvalue()
        block = self.unrollstack(SReturnValue.kind)
        if block is None:
            self.pushvalue(w_returnvalue)   # XXX ping pong
            raise Return
        else:
            unroller = SReturnValue(w_returnvalue)
            next_instr = block.handle(self, unroller)
            return next_instr    # now inside a 'finally' block

    def END_FINALLY(self, oparg, next_instr):
        unroller = self.end_finally()
        if isinstance(unroller, SuspendedUnroller):
            # go on unrolling the stack
            block = self.unrollstack(unroller.kind)
            if block is None:
                w_result = unroller.nomoreblocks()
                self.pushvalue(w_result)
                raise Return
            else:
                next_instr = block.handle(self, unroller)
        return next_instr

    def JUMP_ABSOLUTE(self, jumpto, next_instr):
        return jumpto

    def YIELD_VALUE(self, _, next_instr):
        assert self.pycode.is_generator
        w_result = self.popvalue()
        self.space.do_operation('yield', w_result)
        # XXX yield expressions not supported. This will blow up if the value
        # isn't popped straightaway.
        self.pushvalue(None)

    def SETUP_WITH(self, offsettoend, next_instr):
        # A simpler version than the 'real' 2.7 one:
        # directly call manager.__enter__(), don't use special lookup functions
        # which don't make sense on the RPython type system.
        from pypy.interpreter.pyopcode import WithBlock
        w_manager = self.peekvalue()
        w_exit = self.space.getattr(w_manager, self.space.wrap("__exit__"))
        self.settopvalue(w_exit)
        w_result = self.space.call_method(w_manager, "__enter__")
        block = WithBlock(self, next_instr + offsettoend, self.lastblock)
        self.lastblock = block
        self.pushvalue(w_result)

    def LOAD_GLOBAL(self, nameindex, next_instr):
        w_result = self.space.find_global(self.w_globals, self.getname_u(nameindex))
        self.pushvalue(w_result)

    def BUILD_LIST_FROM_ARG(self, _, next_instr):
        # This opcode was added with pypy-1.8.  Here is a simpler
        # version, enough for annotation.
        last_val = self.popvalue()
        self.pushvalue(self.space.newlist([]))
        self.pushvalue(last_val)

    # XXX Unimplemented 2.7 opcodes ----------------

    # Set literals, set comprehensions

    def BUILD_SET(self, oparg, next_instr):
        raise NotImplementedError("BUILD_SET")

    def SET_ADD(self, oparg, next_instr):
        raise NotImplementedError("SET_ADD")

    # Dict comprehensions

    def MAP_ADD(self, oparg, next_instr):
        raise NotImplementedError("MAP_ADD")

    def make_arguments(self, nargs):
        return ArgumentsForTranslation(self.space, self.peekvalues(nargs))
    def argument_factory(self, *args):
        return ArgumentsForTranslation(self.space, *args)

    def call_contextmanager_exit_function(self, w_func, w_typ, w_val, w_tb):
        if w_typ is not self.space.w_None:
            # The annotator won't allow to merge exception types with None.
            # Replace it with the exception value...
            w_typ = w_val
        self.space.call_function(w_func, w_typ, w_val, w_tb)
        # Return None so that the flow space statically knows that we didn't
        # swallow the exception
        return self.space.w_None

