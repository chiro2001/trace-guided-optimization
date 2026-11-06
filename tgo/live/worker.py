"""自有长驻进程中的工作集与C层回调gate，旧对象与主线程持续复用。"""
import argparse
import concurrent.futures
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from tgo.capabilities import inspect_image
from tgo.elf import ELF as CheckedELF
from tgo.pmu import Group
from tgo.util import read_json,write_json,sha256,cpu_set
from .aarch64_elf import ELF,parse_maps,load_bias
from .loader import reserve,load
from .semantic import run as semantic_gate
from .common import TOKEN_ENV

def threads():
    result=[]
    for path in sorted(Path('/proc/self/task').iterdir(),key=lambda p:int(p.name)):
        result.append({'tid':int(path.name),'comm':(path/'comm').read_text().strip(),
            'cpus':sorted(os.sched_getaffinity(int(path.name))),'schedstat':(path/'schedstat').read_text().strip()})
    return result

def configure_helper(path):
    helper=ctypes.CDLL(str(path),use_errno=True)
    helper.rb_reserve.argtypes=[ctypes.c_uint64,ctypes.c_size_t];helper.rb_reserve.restype=ctypes.c_void_p
    helper.rb_prepare.argtypes=helper.rb_publish.argtypes=[ctypes.c_uint64,ctypes.c_size_t]
    helper.rb_register_frame.argtypes=[ctypes.c_void_p,ctypes.c_size_t];helper.rb_register_frame.restype=ctypes.c_void_p
    helper.rb_find_frame.argtypes=[ctypes.c_uint64];helper.rb_find_frame.restype=ctypes.c_uint64
    if helper.rb_install_sync():raise OSError(ctypes.get_errno(),'指令同步初始化失败')
    return helper

class Worker:
    def __init__(self,args):
        self.workspace=Path(args.workspace).resolve();self.out=self.workspace/'worker';self.out.mkdir(exist_ok=True)
        self.config=read_json(args.config);self.helper_path=Path(args.helper).resolve()
        if not re.fullmatch('[0-9a-f]{48}',os.environ.get(TOKEN_ENV,'')):raise RuntimeError('缺少自有会话身份')
        parent=self.config.get('controller_pid')
        if parent!=os.getppid():raise RuntimeError('控制器父进程身份不符')
        if ctypes.CDLL(None,use_errno=True).prctl(1,15,0,0,0):raise OSError(ctypes.get_errno(),'父进程退出联动初始化失败')
        if parent!=os.getppid():raise RuntimeError('控制器在初始化期间退出')
        self.helper=configure_helper(self.helper_path);self.frame_handles=[];self.requests=0;self.closed=False
        self.main_cpu=self.config.get('main_cpu');self.helper_cpus=set(self.config.get('helper_cpus',[]))
        if self.main_cpu is not None:
            if not self.helper_cpus or self.main_cpu in self.helper_cpus:raise ValueError('主线程与辅助线程CPU集合必须分离')
            os.sched_setaffinity(0,{self.main_cpu})
        self.pool=None;self.adapter=self.config.get('adapter','python-heavy')
        if self.adapter=='native-fixture':self.prepare_native()
        elif self.adapter=='python-heavy':
            from tgo.workloads.python_heavy import prepare
            self.context=prepare(self.config.get('seed',101),self.config.get('size',1024))
        elif self.adapter=='python-stdlib':
            from tgo.workloads.python_stdlib import prepare
            self.context=prepare(self.config.get('seed',101),self.config.get('modules',24))
        else:raise ValueError('未知在线工作集适配器:'+self.adapter)
        for thread in threads():
            if thread['tid']!=os.getpid() and self.helper_cpus:os.sched_setaffinity(thread['tid'],self.helper_cpus)
        source=Path(self.config['image']).resolve();structure=inspect_image(source)
        if not structure['online']['static_candidate']:raise ValueError('在线资格不满足:'+','.join(structure['online']['requirements']))
        elf=ELF(source);mapping=[r for r in parse_maps(Path('/proc/self/maps').read_text()) if r['path']==str(source)]
        if not mapping:raise ValueError('目标没有在本工作进程中加载，禁止另载替代业务状态')
        stat=source.stat()
        if any(row['inode']!=stat.st_ino or row['device']!='%02x:%02x'%(os.major(stat.st_dev),os.minor(stat.st_dev)) for row in mapping):raise RuntimeError('映像文件inode/device与已加载映射不符')
        bias=load_bias(elf,mapping,os.sysconf('SC_PAGE_SIZE'));name=source.name
        self.libraries={name:{'load_bias':bias,'target':str(source),'sha256':sha256(source)}}
        self.manifest={'libraries':{name:{'path':str(source)}}};self.image_name=name
        self.semantic=semantic_gate(self)
        self.initial=self.run_workload(self.config.get('warmup_rounds',1))
        self.context_identity=id(self.context)
        self.object_identity=self.identities()
        os.mkfifo(self.out/'control.fifo',0o600)
        for name in ['probe-ready.fifo','probe-release.fifo']:os.mkfifo(self.out/name,0o600)
        self.probe_ready_fd=os.open(self.out/'probe-ready.fifo',os.O_RDWR)
        self.probe_release_fd=os.open(self.out/'probe-release.fifo',os.O_RDWR)
        self.measure_callback_type=ctypes.PYFUNCTYPE(ctypes.c_int,ctypes.c_void_p)
        self.helper.rb_measure_callback.argtypes=[ctypes.c_int,ctypes.c_int,self.measure_callback_type,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)]
        write_json(self.out/'ready.json',{'schema':'tgo.live-ready.v1','pid':os.getpid(),
            'python_version':sys.version,'python_executable':sys.executable,
            'starttime_ticks':int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]),
            'libraries':self.libraries,'context_identity':self.context_identity,'semantic':self.semantic,
            'object_identity':self.object_identity,
            'threads':threads(),'structure':structure,'ready_monotonic_ns':time.monotonic_ns(),
            'known_input_allowlist':False,'adapter':self.adapter})

    def prepare_native(self):
        self.native=ctypes.CDLL(self.config['image'])
        for name in ['rb_fixture_step','rb_fixture_throw']:
            function=getattr(self.native,name);function.argtypes=[ctypes.c_uint64];function.restype=ctypes.c_uint64
        for name in ['rb_fixture_count','rb_fixture_tls','rb_fixture_state','rb_fixture_identity','rb_fixture_function_address']:getattr(self.native,name).restype=ctypes.c_uint64
        self.context=SimpleNamespace(state=self.native.rb_fixture_state(),function=self.native.rb_fixture_function_address(),count=0)
        def init():
            if self.helper_cpus:os.sched_setaffinity(0,self.helper_cpus)
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=3,initializer=init)
        barrier=threading.Barrier(4);futures=[self.pool.submit(barrier.wait) for _ in range(3)];barrier.wait()
        for future in futures:future.result()

    def run_workload(self,rounds):
        if self.adapter=='python-heavy':
            from tgo.workloads.python_heavy import execute
            result=execute(self.context,self.config.get('case','mixed'),rounds)
        elif self.adapter=='python-stdlib':
            from tgo.workloads.python_stdlib import execute
            result=execute(self.context,rounds)
        else:
            def expected(x):
                y=x+999 if x&15==7 else (x*17)^0x12345678
                return [lambda:y+3,lambda:y^77,lambda:y*3,lambda:y+19,lambda:y^55,lambda:y-9,lambda:y*5,lambda:y+31][x&7]()
            barrier=threading.Barrier(4);count=self.config.get('native_iterations',4000)
            def one(seed):
                before=self.native.rb_fixture_tls();barrier.wait();digest=hashlib.sha256()
                for n in range(count):
                    x=n+seed;actual=self.native.rb_fixture_step(x)
                    if actual!=expected(x):raise RuntimeError('原生公式/异常/虚调用语义不符')
                    digest.update(actual.to_bytes(8,'little'))
                if self.native.rb_fixture_tls()!=before+count:raise RuntimeError('原TLS不连续')
                return digest.hexdigest()
            futures=[self.pool.submit(one,k+1) for k in range(3)];values=[one(0)]+[f.result() for f in futures]
            self.context.count+=count*4
            if self.native.rb_fixture_count()!=self.context.count or self.native.rb_fixture_state()!=self.context.state:raise RuntimeError('原可写状态被复制或改变')
            if self.native.rb_fixture_identity()!=1 or self.native.rb_fixture_function_address()!=self.context.function:raise RuntimeError('函数地址身份发生变化')
            result={'digests':values,'checks':count*4,'function_identity_preserved':True}
        self.requests+=1
        return result

    def identities(self):
        ids=[id(self.context)]
        if self.adapter=='python-heavy':
            nodes,documents=self.context;ids += [id(nodes),id(documents),*[id(node) for node in nodes],*[id(document) for document in documents]]
        elif self.adapter=='python-stdlib':
            for document in self.context:ids += [id(document),*[id(field) for field in document]]
        return hashlib.sha256(json.dumps(ids).encode()).hexdigest()

    def command(self,command):
        operation=command['op']
        if operation=='reserve':return reserve(self,{**command,'name':self.image_name})
        if operation=='load':return load(self,{**command,'name':self.image_name})
        if operation=='semantic':return semantic_gate(self)
        if operation=='sync':
            value=self.helper.rb_sync_threads()
            if value<0:raise OSError(ctypes.get_errno(),'指令同步失败')
            return {'acknowledgements':value,'method':'membarrier' if self.helper.rb_sync_method() else 'signal-isb'}
        if operation=='measure':
            before=threads();self.assert_affinity(before);owner=id(self.context);request_before=self.requests
            if self.identities()!=self.object_identity:raise RuntimeError('原工作集对象图身份改变')
            group=Group();group.start();results=[];errors=[]
            def body(_context):
                try:results.append(self.run_workload(self.config.get('rounds',100)));return 0
                except BaseException as error:errors.append(str(error));return -1
            callback=self.measure_callback_type(body);times=(ctypes.c_uint64*2)()
            probing=command.get('probe',False)
            try:
                status=self.helper.rb_measure_callback(self.probe_ready_fd if probing else -1,self.probe_release_fd if probing else -1,callback,None,times)
                counters=group.stop()
            finally:group.close()
            if errors:raise RuntimeError(errors[0])
            if status or not results:raise RuntimeError('C层测量回调失败')
            start,end=times;result=results[0]
            after=threads()
            self.assert_affinity(after)
            if {t['tid'] for t in before}!={t['tid'] for t in after}:raise RuntimeError('ROI线程集合改变')
            if owner!=id(self.context) or owner!=self.context_identity:raise RuntimeError('工作集对象身份改变')
            if self.identities()!=self.object_identity:raise RuntimeError('原工作集对象图身份改变')
            return {'wall_ns':end-start,'output_sha256':hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest(),
                'business_start_monotonic_ns':start,'business_end_monotonic_ns':end,
                'context_identity':owner,'request_before':request_before,'request_after':self.requests,
                'object_identity':self.object_identity,
                'threads_before_raw':before,'threads_after_raw':after,'pmu':counters,'result':result}
        if operation=='exit':self.closed=True;return {'exited':True}
        raise ValueError('未知在线操作:'+operation)

    def assert_affinity(self,rows):
        if self.main_cpu is None:return
        for row in rows:
            allowed={self.main_cpu} if row['tid']==os.getpid() else self.helper_cpus
            if not row['cpus'] or not set(row['cpus'])<=allowed:raise RuntimeError('线程CPU越出约定分区')

    def serve(self):
        path=self.out/'control.fifo';fd=os.open(path,os.O_RDWR)
        callback_type=ctypes.PYFUNCTYPE(ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t)
        def callback(pointer,size):
            command=None
            try:
                command=json.loads(ctypes.string_at(pointer,size))
                if not re.fullmatch('[0-9a-f]{32}',str(command.get('id',''))):return 1
                if command.get('token')!=os.environ.get(TOKEN_ENV):return 1
                response={'status':'ok','result':self.command(command)}
            except BaseException as error:
                if not isinstance(command,dict) or not re.fullmatch('[0-9a-f]{32}',str(command.get('id',''))):return 1
                response={'status':'error','error':str(error),'traceback':traceback.format_exc()}
            write_json(self.out/('done-'+command['id']+'.json'),response)
            return 0 if self.closed else 1
        self.callback=callback_type(callback);self.helper.rb_fifo_loop.argtypes=[ctypes.c_int,callback_type]
        try:self.helper.rb_fifo_loop(fd,self.callback)
        finally:
            os.close(fd)
            if self.pool:self.pool.shutdown()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--workspace',required=True);parser.add_argument('--config',required=True);parser.add_argument('--helper',required=True)
    args=parser.parse_args()
    try:Worker(args).serve()
    except BaseException as error:
        out=Path(args.workspace)/'worker';out.mkdir(exist_ok=True)
        write_json(out/'error.json',{'error':str(error),'traceback':traceback.format_exc()});raise
if __name__=='__main__':main()
