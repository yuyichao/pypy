from rpython.rtyper.lltypesystem import rffi, lltype, llmemory
from rpython.translator.tool.cbuild import ExternalCompilationInfo
from rpython.translator import cdir
import py
from rpython.rlib import jit, rgc
from rpython.rlib.debug import ll_assert
from rpython.rlib.objectmodel import we_are_translated, specialize
from rpython.rtyper.lltypesystem.lloperation import llop
from rpython.rtyper.tool import rffi_platform

class error(Exception):
    pass

translator_c_dir = py.path.local(cdir)

eci = ExternalCompilationInfo(
    includes = ['src/thread.h'],
    separate_module_files = [translator_c_dir / 'src' / 'thread.c'],
    include_dirs = [translator_c_dir],
    export_symbols = ['RPyThreadGetIdent', 'RPyThreadLockInit',
                      'RPyThreadAcquireLock', 'RPyThreadAcquireLockTimed',
                      'RPyThreadReleaseLock',
                      'RPyThreadGetStackSize', 'RPyThreadSetStackSize',
                      'RPyOpaqueDealloc_ThreadLock',
                      'RPyThreadAfterFork']
)

def llexternal(name, args, result, **kwds):
    kwds.setdefault('sandboxsafe', True)
    return rffi.llexternal(name, args, result, compilation_info=eci,
                           **kwds)

def _emulated_start_new_thread(func):
    "NOT_RPYTHON"
    import thread
    try:
        ident = thread.start_new_thread(func, ())
    except thread.error:
        ident = -1
    return rffi.cast(rffi.LONG, ident)

CALLBACK = lltype.Ptr(lltype.FuncType([], lltype.Void))
c_thread_start = llexternal('RPyThreadStart', [CALLBACK], rffi.LONG,
                            _callable=_emulated_start_new_thread,
                            releasegil=True)  # release the GIL, but most
                                              # importantly, reacquire it
                                              # around the callback
c_thread_get_ident = llexternal('RPyThreadGetIdent', [], rffi.LONG,
                                _nowrapper=True)    # always call directly

TLOCKP = rffi.COpaquePtr('struct RPyOpaque_ThreadLock',
                          compilation_info=eci)
TLOCKP_SIZE = rffi_platform.sizeof('struct RPyOpaque_ThreadLock', eci)
c_thread_lock_init = llexternal('RPyThreadLockInit', [TLOCKP], rffi.INT,
                                releasegil=False)   # may add in a global list
c_thread_lock_dealloc_NOAUTO = llexternal('RPyOpaqueDealloc_ThreadLock',
                                          [TLOCKP], lltype.Void,
                                          _nowrapper=True)
c_thread_acquirelock = llexternal('RPyThreadAcquireLock', [TLOCKP, rffi.INT],
                                  rffi.INT,
                                  releasegil=True)    # release the GIL
c_thread_acquirelock_timed = llexternal('RPyThreadAcquireLockTimed',
                                        [TLOCKP, rffi.LONGLONG, rffi.INT],
                                        rffi.INT,
                                        releasegil=True)    # release the GIL
c_thread_releaselock = llexternal('RPyThreadReleaseLock', [TLOCKP], lltype.Void,
                                  releasegil=True)    # release the GIL

# another set of functions, this time in versions that don't cause the
# GIL to be released.  To use to handle the GIL lock itself.
c_thread_acquirelock_NOAUTO = llexternal('RPyThreadAcquireLock',
                                         [TLOCKP, rffi.INT], rffi.INT,
                                         _nowrapper=True)
c_thread_releaselock_NOAUTO = llexternal('RPyThreadReleaseLock',
                                         [TLOCKP], lltype.Void,
                                         _nowrapper=True)


def allocate_lock():
    return Lock(allocate_ll_lock())

@specialize.arg(0)
def ll_start_new_thread(func):
    ident = c_thread_start(func)
    if ident == -1:
        raise error("can't start new thread")
    return ident

# wrappers...

@jit.loop_invariant
def get_ident():
    return rffi.cast(lltype.Signed, c_thread_get_ident())

@specialize.arg(0)
def start_new_thread(x, y):
    """In RPython, no argument can be passed.  You have to use global
    variables to pass information to the new thread.  That's not very
    nice, but at least it avoids some levels of GC issues.
    """
    assert len(y) == 0
    return rffi.cast(lltype.Signed, ll_start_new_thread(x))

class DummyLock(object):
    def acquire(self, flag):
        return True

    def release(self):
        pass

    def _freeze_(self):
        return True

    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass

dummy_lock = DummyLock()

class Lock(object):
    """ Container for low-level implementation
    of a lock object
    """
    _immutable_fields_ = ["_lock"]

    def __init__(self, ll_lock):
        self._lock = ll_lock

    def acquire(self, flag):
        res = c_thread_acquirelock(self._lock, int(flag))
        res = rffi.cast(lltype.Signed, res)
        return bool(res)

    def acquire_timed(self, timeout):
        """Timeout is in microseconds.  Returns 0 in case of failure,
        1 in case it works, 2 if interrupted by a signal."""
        res = c_thread_acquirelock_timed(self._lock, timeout, 1)
        res = rffi.cast(lltype.Signed, res)
        return res

    def release(self):
        # Sanity check: the lock must be locked
        if self.acquire(False):
            c_thread_releaselock(self._lock)
            raise error("bad lock")
        else:
            c_thread_releaselock(self._lock)

    def __del__(self):
        if free_ll_lock is None:  # happens when tests are shutting down
            return
        free_ll_lock(self._lock)

    def __enter__(self):
        self.acquire(True)

    def __exit__(self, *args):
        self.release()

    def _cleanup_(self):
        raise Exception("seeing a prebuilt rpython.rlib.rthread.Lock instance")

# ____________________________________________________________
#
# Stack size

get_stacksize = llexternal('RPyThreadGetStackSize', [], lltype.Signed)
set_stacksize = llexternal('RPyThreadSetStackSize', [lltype.Signed],
                           lltype.Signed)

# ____________________________________________________________
#
# Hack

thread_after_fork = llexternal('RPyThreadAfterFork', [], lltype.Void)

# ____________________________________________________________
#
# GIL support wrappers

null_ll_lock = lltype.nullptr(TLOCKP.TO)

def allocate_ll_lock():
    # track_allocation=False here; be careful to lltype.free() it.  The
    # reason it is set to False is that we get it from all app-level
    # lock objects, as well as from the GIL, which exists at shutdown.
    ll_lock = lltype.malloc(TLOCKP.TO, flavor='raw', track_allocation=False)
    res = c_thread_lock_init(ll_lock)
    if rffi.cast(lltype.Signed, res) <= 0:
        lltype.free(ll_lock, flavor='raw', track_allocation=False)
        raise error("out of resources")
    # Add some memory pressure for the size of the lock because it is an
    # Opaque object
    rgc.add_memory_pressure(TLOCKP_SIZE)
    return ll_lock

def free_ll_lock(ll_lock):
    acquire_NOAUTO(ll_lock, False)
    release_NOAUTO(ll_lock)
    c_thread_lock_dealloc_NOAUTO(ll_lock)
    lltype.free(ll_lock, flavor='raw', track_allocation=False)

def acquire_NOAUTO(ll_lock, flag):
    flag = rffi.cast(rffi.INT, int(flag))
    res = c_thread_acquirelock_NOAUTO(ll_lock, flag)
    res = rffi.cast(lltype.Signed, res)
    return bool(res)

def release_NOAUTO(ll_lock):
    if not we_are_translated():
        ll_assert(not acquire_NOAUTO(ll_lock, False), "NOAUTO lock not held!")
    c_thread_releaselock_NOAUTO(ll_lock)

# ____________________________________________________________
#
# Thread integration.
# These are five completely ad-hoc operations at the moment.

@jit.dont_look_inside
def gc_thread_run():
    """To call whenever the current thread (re-)acquired the GIL.
    """
    if we_are_translated():
        llop.gc_thread_run(lltype.Void)
gc_thread_run._always_inline_ = True

@jit.dont_look_inside
def gc_thread_start():
    """To call at the beginning of a new thread.
    """
    if we_are_translated():
        llop.gc_thread_start(lltype.Void)

@jit.dont_look_inside
def gc_thread_die():
    """To call just before the final GIL release done by a dying
    thread.  After a thread_die(), no more gc operation should
    occur in this thread.
    """
    if we_are_translated():
        llop.gc_thread_die(lltype.Void)
gc_thread_die._always_inline_ = True

@jit.dont_look_inside
def gc_thread_before_fork():
    """To call just before fork().  Prepares for forking, after
    which only the current thread will be alive.
    """
    if we_are_translated():
        return llop.gc_thread_before_fork(llmemory.Address)
    else:
        return llmemory.NULL

@jit.dont_look_inside
def gc_thread_after_fork(result_of_fork, opaqueaddr):
    """To call just after fork().
    """
    if we_are_translated():
        llop.gc_thread_after_fork(lltype.Void, result_of_fork, opaqueaddr)
    else:
        assert opaqueaddr == llmemory.NULL

# ____________________________________________________________
#
# Thread-locals.  Only for references that change "not too often" --
# for now, the JIT compiles get() as a loop-invariant, so basically
# don't change them.
# KEEP THE REFERENCE ALIVE, THE GC DOES NOT FOLLOW THEM SO FAR!
# We use _make_sure_does_not_move() to make sure the pointer will not move.

ecitl = ExternalCompilationInfo(
    includes = ['src/threadlocal.h'],
    separate_module_files = [translator_c_dir / 'src' / 'threadlocal.c'])
ensure_threadlocal = rffi.llexternal_use_eci(ecitl)

class ThreadLocalReference(object):
    _COUNT = 1
    OPAQUEID = lltype.OpaqueType("ThreadLocalRef",
                                 hints={"threadlocalref": True,
                                        "external": "C",
                                        "c_name": "RPyThreadStaticTLS"})

    def __init__(self, Cls):
        "NOT_RPYTHON: must be prebuilt"
        import thread
        self.Cls = Cls
        self.local = thread._local()      # <- NOT_RPYTHON
        unique_id = ThreadLocalReference._COUNT
        ThreadLocalReference._COUNT += 1
        opaque_id = lltype.opaqueptr(ThreadLocalReference.OPAQUEID,
                                     'tlref%d' % unique_id)
        self.opaque_id = opaque_id

        def get():
            if we_are_translated():
                from rpython.rtyper.lltypesystem import rclass
                from rpython.rtyper.annlowlevel import cast_base_ptr_to_instance
                ptr = llop.threadlocalref_get(rclass.OBJECTPTR, opaque_id)
                return cast_base_ptr_to_instance(Cls, ptr)
            else:
                return getattr(self.local, 'value', None)

        @jit.dont_look_inside
        def set(value):
            assert isinstance(value, Cls) or value is None
            if we_are_translated():
                from rpython.rtyper.annlowlevel import cast_instance_to_base_ptr
                from rpython.rlib.rgc import _make_sure_does_not_move
                from rpython.rlib.objectmodel import running_on_llinterp
                ptr = cast_instance_to_base_ptr(value)
                if not running_on_llinterp:
                    gcref = lltype.cast_opaque_ptr(llmemory.GCREF, ptr)
                    _make_sure_does_not_move(gcref)
                llop.threadlocalref_set(lltype.Void, opaque_id, ptr)
                ensure_threadlocal()
            else:
                self.local.value = value

        self.get = get
        self.set = set

    def _freeze_(self):
        return True
