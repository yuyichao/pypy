from rpython.rlib.clibffi import FFI_DEFAULT_ABI
from rpython.rlib.objectmodel import we_are_translated
from rpython.jit.metainterp.history import INT, FLOAT
from rpython.jit.backend.arm.arch import WORD
from rpython.jit.backend.arm import registers as r
from rpython.jit.backend.arm.jump import remap_frame_layout
from rpython.jit.backend.llsupport.callbuilder import AbstractCallBuilder
from rpython.jit.backend.arm.helper.assembler import count_reg_args
from rpython.jit.backend.arm.helper.regalloc import check_imm_arg


class ARMCallbuilder(AbstractCallBuilder):
    def __init__(self, assembler, fnloc, arglocs,
                 resloc=r.r0, restype=INT, ressize=WORD, ressigned=True):
        AbstractCallBuilder.__init__(self, assembler, fnloc, arglocs,
                                     resloc, restype, ressize)
        self.current_sp = 0

    def push_gcmap(self):
        assert not self.is_call_release_gil
        # we push *now* the gcmap, describing the status of GC registers
        # after the rearrangements done just above, ignoring the return
        # value eax, if necessary
        noregs = self.asm.cpu.gc_ll_descr.is_shadow_stack()
        gcmap = self.asm._regalloc.get_gcmap([r.r0], noregs=noregs)
        self.asm.push_gcmap(self.mc, gcmap, store=True)

    def pop_gcmap(self):
        assert not self.is_call_release_gil
        self.asm._reload_frame_if_necessary(self.mc)
        self.asm.pop_gcmap(self.mc)

    def emit_raw_call(self):
        #the actual call
        if self.fnloc.is_imm():
            self.mc.BL(self.fnloc.value)
            return
        if self.fnloc.is_stack():
            self.asm.mov_loc_loc(self.fnloc, r.ip)
            self.fnloc = r.ip
        assert self.fnloc.is_reg()
        self.mc.BLX(self.fnloc.value)

    def restore_stack_pointer(self):
        # readjust the sp in case we passed some args on the stack
        assert self.current_sp % 8 == 0  # sanity check
        if self.current_sp != 0:
            self._adjust_sp(self.current_sp)
        self.current_sp = 0

    def _push_stack_args(self, stack_args, on_stack):
        assert on_stack % 8 == 0
        #then we push every thing on the stack
        for i in range(len(stack_args) - 1, -1, -1):
            arg = stack_args[i]
            if arg is None:
                self.mc.PUSH([r.ip.value])
            else:
                self.asm.regalloc_push(arg)
        self.current_sp -= on_stack

    def _adjust_sp(self, n):
        assert n < 0
        n = abs(n)

        if check_imm_arg(n):
            self.mc.ADD_ri(r.sp.value, r.sp.value, n)
        else:
            self.mc.gen_load_int(r.ip.value, n, cond=fcond)
            self.mc.ADD_rr(r.sp.value, r.sp.value, r.ip.value, cond=fcond)


class SoftFloatCallBuilder(ARMCallbuilder):

    def load_result(self):
        resloc = self.resloc
        # ensure the result is wellformed and stored in the correct location
        if resloc is not None:
            if resloc.is_vfp_reg():
                # move result to the allocated register
                self.asm.mov_to_vfp_loc(r.r0, r.r1, resloc)
            elif resloc.is_reg():
                self.asm._ensure_result_bit_extension(resloc,
                                                  self.ressize, self.ressigned)


    def _collect_stack_args(self, arglocs):
        n_args = len(arglocs)
        reg_args = count_reg_args(arglocs)
        # all arguments past the 4th go on the stack
        # first we need to prepare the list so it stays aligned
        stack_args = []
        count = 0
        on_stack = 0
        if n_args > reg_args:
            for i in range(reg_args, n_args):
                arg = arglocs[i]
                if arg.type != FLOAT:
                    count += 1
                    on_stack += 1
                else:
                    on_stack += 2
                    if count % 2 != 0:
                        stack_args.append(None)
                        count = 0
                        on_stack += 1
                stack_args.append(arg)
            if count % 2 != 0:
                on_stack += 1
                stack_args.append(None)
        if on_stack > 0:
            self._push_stack_args(stack_args, on_stack*WORD)

    def prepare_arguments(self):
        reg_args = count_reg_args(arglocs)
        self._collect_and_push_stack_args(arglocs)
        # collect variables that need to go in registers and the registers they
        # will be stored in
        num = 0
        count = 0
        non_float_locs = []
        non_float_regs = []
        float_locs = []
        for i in range(reg_args):
            arg = arglocs[i]
            if arg.type == FLOAT and count % 2 != 0:
                    num += 1
                    count = 0
            reg = r.caller_resp[num]

            if arg.type == FLOAT:
                float_locs.append((arg, reg))
            else:
                non_float_locs.append(arg)
                non_float_regs.append(reg)

            if arg.type == FLOAT:
                num += 2
            else:
                num += 1
                count += 1
        # Check that the address of the function we want to call is not
        # currently stored in one of the registers used to pass the arguments.
        # If this happens to be the case we remap the register to r4 and use r4
        # to call the function
        if self.fnloc in non_float_regs:
            non_float_locs.append(self.fnloc)
            non_float_regs.append(r.r4)
            self.fnloc = r.r4
        # remap values stored in core registers
        remap_frame_layout(self.asm, non_float_locs, non_float_regs, r.ip)

        for loc, reg in float_locs:
            self.asm.mov_from_vfp_loc(loc, reg, r.all_regs[reg.value + 1])

class HardFloatCallBuilder(ARMCallbuilder):

    def prepare_arguments(self):
        non_float_locs = []
        non_float_regs = []
        float_locs = []
        float_regs = []
        stack_args = []

        arglocs = self.arglocs
        argtypes = self.argtypes

        count = 0                      # stack alignment counter
        on_stack = 0
        for arg in arglocs:
            if arg.type != FLOAT:
                if len(non_float_regs) < len(r.argument_regs):
                    reg = r.argument_regs[len(non_float_regs)]
                    non_float_locs.append(arg)
                    non_float_regs.append(reg)
                else:  # non-float argument that needs to go on the stack
                    count += 1
                    on_stack += 1
                    stack_args.append(arg)
            else:
                if len(float_regs) < len(r.vfp_argument_regs):
                    reg = r.vfp_argument_regs[len(float_regs)]
                    float_locs.append(arg)
                    float_regs.append(reg)
                else:  # float argument that needs to go on the stack
                    if count % 2 != 0:
                        stack_args.append(None)
                        count = 0
                        on_stack += 1
                    stack_args.append(arg)
                    on_stack += 2
        # align the stack
        if count % 2 != 0:
            stack_args.append(None)
            on_stack += 1
        self._push_stack_args(stack_args, on_stack*WORD)
        # Check that the address of the function we want to call is not
        # currently stored in one of the registers used to pass the arguments.
        # If this happens to be the case we remap the register to r4 and use r4
        # to call the function
        if self.fnloc in non_float_regs:
            non_float_locs.append(self.fnloc)
            non_float_regs.append(r.r4)
            self.fnloc = r.r4
        # remap values stored in core registers
        remap_frame_layout(self.asm, non_float_locs, non_float_regs, r.ip)
        # remap values stored in vfp registers
        remap_frame_layout(self.asm, float_locs, float_regs, r.vfp_ip)

    def load_result(self):
        resloc = self.resloc
        # ensure the result is wellformed and stored in the correct location
        if resloc is not None and resloc.is_reg():
            self.asm._ensure_result_bit_extension(resloc,
                                                  self.ressize, self.ressigned)



def get_callbuilder(cpu, assembler, fnloc, arglocs,
                 resloc=r.r0, restype=INT, ressize=WORD, ressigned=True):
    if cpu.cpuinfo.hf_abi:
        return HardFloatCallBuilder(assembler, fnloc, arglocs, resloc,
                                        restype, ressize, ressigned)
    else:
        return SoftFloatCallBuilder(assembler, fnloc, arglocs, resloc,
                                        restype, ressize, ressigned)
