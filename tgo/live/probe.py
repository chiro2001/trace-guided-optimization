"""C层先停车再布置硬件断点，确保命中发生在被测业务区间内。"""
import ctypes
import os
import signal
import struct
import time
from .ptrace import Ptrace,ProcFS,IOVec

NT_ARM_HW_BREAK=0x402
WALL=0x40000000

def get_hw(trace,pid):
    data=ctypes.create_string_buffer(264);iov=IOVec(ctypes.addressof(data),264)
    trace.request(0x4204,pid,NT_ARM_HW_BREAK,ctypes.addressof(iov));return data.raw[:iov.length]

def set_hw(trace,pid,data):
    buffer=ctypes.create_string_buffer(data);iov=IOVec(ctypes.addressof(buffer),len(data))
    trace.request(0x4205,pid,NT_ARM_HW_BREAK,ctypes.addressof(iov))

def prove(session,address,timeout=120):
    session.identity();pid=session.pid;trace=Ptrace();attached=False;stopped=False;saved=None
    ready=os.open(session.out/'worker/probe-ready.fifo',os.O_RDONLY|os.O_NONBLOCK)
    try:
        try:
            while os.read(ready,4096):pass
        except BlockingIOError:pass
        identifier=session.send('measure',probe=True);deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                if os.read(ready,1)==b'R':break
            except BlockingIOError:pass
            if session.process.poll() is not None:raise RuntimeError('worker在探针gate前退出')
            time.sleep(.002)
        else:raise TimeoutError('业务探针gate未就绪')
        trace.request(0x4206,pid);attached=True;trace.request(0x4207,pid)
        waited,status=os.waitpid(pid,WALL)
        if waited!=pid or not os.WIFSTOPPED(status):raise RuntimeError('探针线程未停车')
        stopped=True;saved=get_hw(trace,pid)
        if len(saved)<24 or struct.unpack_from('<I',saved)[0]&255==0:raise RuntimeError('无硬件执行断点能力')
        slots=struct.unpack_from('<I',saved)[0]&255
        if any(struct.unpack_from('<I',saved,offset)[0]&1 for offset in range(16,min(len(saved),8+slots*16),16)):
            raise RuntimeError('已有活动硬件断点')
        saved=saved[:24];configured=bytearray(saved);struct.pack_into('<QII',configured,8,address,0x1e5,0)
        set_hw(trace,pid,bytes(configured));trace.request(7,pid);stopped=False
        release=os.open(session.out/'worker/probe-release.fifo',os.O_WRONLY|os.O_NONBLOCK)
        try:os.write(release,b'G')
        finally:os.close(release)
        while time.monotonic()<deadline:
            waited,status=os.waitpid(pid,os.WNOHANG|WALL)
            if waited:
                if not os.WIFSTOPPED(status):raise RuntimeError('worker在硬件探针期间退出')
                stopped=True;pc=trace.pc(pid)
                if os.WSTOPSIG(status)!=signal.SIGTRAP or pc!=address:raise RuntimeError('非预期的硬件探针停止')
                observed=time.monotonic_ns();set_hw(trace,pid,saved);trace.request(17,pid);attached=False;stopped=False
                result=session.receive(identifier,timeout)
                if not result['business_start_monotonic_ns']<=observed<=result['business_end_monotonic_ns']:
                    raise RuntimeError('新代码命中不在业务ROI内')
                proof={'pc':pc,'expected_new_entry':address,'tid':pid,'monotonic_ns':observed,
                    'within_business_roi':True,'method':'AArch64 hardware execute breakpoint after C-level ROI gate'}
                session.record.setdefault('hardware_execution_proofs',[]).append(proof);session.save()
                return result,proof
            if session.process.poll() is not None:raise RuntimeError('worker在探针期间退出')
            if (session.out/'worker'/('done-'+identifier+'.json')).exists():raise RuntimeError('业务已完成，未命中候选新入口')
            time.sleep(.002)
        raise TimeoutError('硬件探针未命中')
    finally:
        os.close(ready)
        if attached:
            if not stopped:trace.request(0x4207,pid);os.waitpid(pid,WALL)
            if saved is not None:set_hw(trace,pid,saved)
            trace.request(17,pid)
