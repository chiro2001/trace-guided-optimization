"""全线程停车原语，提取自已验证的页事务实现；无应用文件白名单。"""
import contextlib,ctypes,errno,os,signal,struct,time
from pathlib import Path
MASK64=(1<<64)-1
PTRACE_GETREGSET,PTRACE_SEIZE,PTRACE_INTERRUPT,PTRACE_DETACH=0x4204,0x4206,0x4207,17
WALL,EVENT_STOP,NT_PRSTATUS=0x40000000,128,1
class PatchError(RuntimeError):
    pass

class ProcFS:
    def __init__(self, root='/proc'):
        self.root = Path(root)

    def path(self, pid, suffix):
        return self.root / str(pid) / suffix

    def starttime(self, pid):
        return int(self.path(pid, 'stat').read_text().rsplit(')', 1)[1].split()[19])

    def status(self, pid, tid=None):
        suffix = 'status' if tid is None else f'task/{tid}/status'
        return dict((k, v.strip()) for k, v in (line.split(':', 1) for line in self.path(pid, suffix).read_text().splitlines() if ':' in line))

    def tasks(self, pid):
        return {int(p.name) for p in self.path(pid, 'task').iterdir() if p.name.isdigit()}

    def environment(self, pid):
        pairs = [v.split(b'=', 1) for v in self.path(pid, 'environ').read_bytes().split(b'\0') if b'=' in v]
        if len({p[0] for p in pairs}) != len(pairs):
            raise PatchError('重复环境变量')
        return dict(pairs)

    def maps(self, pid):
        return self.path(pid, 'maps').read_bytes()

class IOVec(ctypes.Structure):
    _fields_ = [('base', ctypes.c_void_p), ('length', ctypes.c_size_t)]

def decode_aarch64_pc(raw):
    if len(raw) != 34 * 8:
        raise PatchError('NT_PRSTATUS不是完整AArch64 user_pt_regs')
    return struct.unpack_from('<Q', raw, 32 * 8)[0]

class Ptrace:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.ptrace.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self.libc.ptrace.restype = ctypes.c_long

    def request(self, operation, tid, address=0, data=0):
        ctypes.set_errno(0)
        value = self.libc.ptrace(operation, tid, ctypes.c_void_p(address), ctypes.c_void_p(data))
        error = ctypes.get_errno()
        if value == -1 and error:
            raise OSError(error, os.strerror(error))
        return value & MASK64

    def pc(self, tid):
        buffer = ctypes.create_string_buffer(272)
        iov = IOVec(ctypes.addressof(buffer), 272)
        self.request(PTRACE_GETREGSET, tid, NT_PRSTATUS, ctypes.addressof(iov))
        return decode_aarch64_pc(buffer.raw[:iov.length])

@contextlib.contextmanager
def blocked_cleanup_signals():
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)

class StopWorld:
    def __init__(self, plan, proc, ptrace, events, timeout=5):
        self.plan, self.proc, self.ptrace, self.events, self.timeout = plan, proc, ptrace, events, timeout
        self.attached = {}

    def wait(self, tid, deadline):
        while True:
            waited, status = os.waitpid(tid, os.WNOHANG | WALL)
            if waited:
                if not os.WIFSTOPPED(status):
                    raise PatchError('停车时线程退出')
                sig, event = os.WSTOPSIG(status), status >> 16
                self.attached[tid] = dict(stopped=True, signal=0 if event == EVENT_STOP and sig == signal.SIGTRAP else sig)
                self.events.append(dict(event='thread_stopped', tid=tid, signal=self.attached[tid]['signal'], monotonic_ns=time.monotonic_ns()))
                return
            if time.monotonic() >= deadline:
                raise PatchError('等待ptrace-stop超时')
            time.sleep(.002)

    def __enter__(self):
        deadline, stable, pid = time.monotonic() + self.timeout, 0, self.plan['pid']
        try:
            while stable < 2:
                if time.monotonic() > deadline or self.proc.starttime(pid) != self.plan['starttime_ticks']:
                    raise PatchError('停车超时/PID改变')
                tids = self.proc.tasks(pid)
                if not tids:
                    raise PatchError('目标无线程')
                for tid in sorted(tids - self.attached.keys()):
                    status = self.proc.status(pid, tid)
                    if int(status['TracerPid']) or status['State'].split()[0] in ('T', 't') or int(status['Tgid']) != pid:
                        raise PatchError('线程已有tracer/stop或TGID不同')
                    self.ptrace.request(PTRACE_SEIZE, tid)
                    self.attached[tid] = dict(stopped=False, signal=0)
                    self.ptrace.request(PTRACE_INTERRUPT, tid)
                for tid, state in list(self.attached.items()):
                    if not state['stopped']:
                        self.wait(tid, deadline)
                after = self.proc.tasks(pid)
                if after != self.attached.keys():
                    if not self.attached.keys() <= after:
                        raise PatchError('停车期间线程消失')
                    stable = 0
                    continue
                if any(int(self.proc.status(pid, tid)['TracerPid']) != os.getpid() or self.proc.status(pid, tid)['State'].split()[0] not in ('T', 't') for tid in after):
                    raise PatchError('未确认全线程真实停止')
                stable += 1
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        errors = []
        with blocked_cleanup_signals():
            for tid, state in reversed(list(self.attached.items())):
                try:
                    if not state['stopped']:
                        self.ptrace.request(PTRACE_INTERRUPT, tid)
                        self.wait(tid, time.monotonic() + 2)
                        state = self.attached[tid]
                    self.ptrace.request(PTRACE_DETACH, tid, 0, state['signal'])
                    self.events.append(dict(event='thread_detached', tid=tid, signal=state['signal'], monotonic_ns=time.monotonic_ns()))
                    del self.attached[tid]
                except (ProcessLookupError, ChildProcessError):
                    del self.attached[tid]
                except OSError as error:
                    if error.errno == errno.ESRCH:
                        self.attached.pop(tid, None)
                    else:
                        errors.append(str(error))
                except BaseException as error:
                    errors.append(str(error))
        if errors:
            raise PatchError('detach未完全确认，CLI必须退出: ' + str(errors))

    def __exit__(self, kind, value, traceback):
        self.close()
