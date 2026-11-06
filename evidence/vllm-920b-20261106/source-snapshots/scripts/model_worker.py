#!/usr/bin/env python3
"""完整1002-token业务门禁，预置加载助手及请求间gate。"""
from __future__ import annotations
import argparse,ctypes,hashlib,json,os,sys,threading,time,traceback
from pathlib import Path
from common import *
sys.path.insert(0,str(ROOT/'reference'))
from elf_aarch64 import ELF,parse_maps
import measure_roi

def affinities():
    rows = []
    for p in Path("/proc/self/task").iterdir():
        try:
            rows.append({"tid": int(p.name), "comm": (p / "comm").read_text().strip(),
                         "cpus": sorted(os.sched_getaffinity(int(p.name))),
                         "schedstat": (p / "schedstat").read_text().strip()})
        except (FileNotFoundError, ProcessLookupError):
            pass
    return sorted(rows, key=lambda r: r["tid"])

def pin_existing():
    main = threading.get_native_id()
    for row in affinities():
        try:
            os.sched_setaffinity(row["tid"], {MAIN_CPU} if row["tid"] == main else HELPER_CPUS)
        except ProcessLookupError:
            pass

def assert_affinities(rows):
    main = threading.get_native_id()
    for row in rows:
        expected = {MAIN_CPU} if row["tid"] == main else HELPER_CPUS
        if not set(row["cpus"]) <= expected:
            raise RuntimeError("原始亲和性越界：" + str(row))

class Worker:

    def __init__(self,arm,out):
        self.arm,self.out,self.mode=arm,out,'model'
        self.manifest=json.loads((ROOT/'inputs/matrix.json').read_text())[arm]
        self.steps,self.worker_tids=[],set()
        self.active=False
        self.helper=ctypes.CDLL(os.environ.get('RBF_LOADER_PATH',str(ROOT/'native/libreplay_loader.so')),use_errno=True)
        self.helper.rb_install_sync.restype=ctypes.c_int
        self.helper.rb_reserve.argtypes=[ctypes.c_uint64,ctypes.c_size_t]
        self.helper.rb_reserve.restype=ctypes.c_void_p
        self.helper.rb_prepare.argtypes=self.helper.rb_publish.argtypes=[ctypes.c_uint64,ctypes.c_size_t]
        self.helper.rb_register_frame.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
        self.helper.rb_register_frame.restype=ctypes.c_void_p
        self.helper.rb_find_frame.argtypes=[ctypes.c_uint64]
        self.helper.rb_find_frame.restype=ctypes.c_uint64
        if self.helper.rb_install_sync():raise OSError(ctypes.get_errno(),'ISB信号安装失败')
        self.frame_handles=[]
        self.initialize_model()
        pin_existing()
        self.warm_sha=self.generate(50)['output_sha256']
        self.full_sha=self.generate(1002)['output_sha256']
        pin_existing()
        measure_roi.EVENTS=[measure_roi.Event('cycles',0,0),measure_roi.Event('instructions',0,1)]
        self.perf=measure_roi.PerfGroup(scope='thread')
        if self.perf.open_errors:raise RuntimeError(str(self.perf.open_errors))
        rows=affinities();assert_affinities(rows)
        self.libraries={}
        maps=Path('/proc/self/maps').read_text()
        (self.out/'maps-ready.txt').write_text(maps)
        for name,target in TARGETS.items():
            actual=sha(target)
            if actual!=self.manifest['libraries'][name]['sha256']:raise RuntimeError('输入SHA不符:'+name)
            elf=ELF(target)
            mappings=[r for r in parse_maps(maps) if r['path']==target and r['offset']==0]
            load=min(elf.loads,key=lambda r:r['vaddr'])
            if len(mappings)!=1:raise RuntimeError('库映射有歧义:'+name)
            bias=mappings[0]['start']-(load['vaddr']&~4095)
            self.libraries[name]={'sha256':actual,'load_bias':bias,'target':target}
        self.ready={'pid':os.getpid(),'starttime_ticks':int(Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]),
            'arm':arm,'libraries':self.libraries,'llm_identity':id(self.llm),
            'thread_affinities_raw':rows,'sources':self.sources(),'full_sha256':self.full_sha,
            'ready_monotonic_ns':time.monotonic_ns()}
        atomic_json(self.out/'ready.json',self.ready)

    def sources(self):
        observed={name:sha(target) for name,target in SOURCES.items()}
        if observed!=self.manifest['sources']:raise RuntimeError('源码SHA不符')
        return observed

    def initialize_model(self):
            if list(Path("/dev").glob("davinci*")):
                raise RuntimeError("只允许设备桩")
            self.sources()
            import torch
            from vllm import LLM, SamplingParams
            from vllm_ascend.worker.worker import NPUWorker
            self.torch, self.SamplingParams = torch, SamplingParams
            self.early_handles=[]
            if os.environ.get('RBF_EARLY_TRANSFORMER')=='1':
                self.early_handles.append(ctypes.CDLL(TARGETS['libophost_transformer.so'],mode=ctypes.RTLD_GLOBAL|os.RTLD_NOW))
            original = NPUWorker.execute_model
            def counted(worker, schedule):
                if self.active:
                    self.steps.append(int(schedule.total_num_scheduled_tokens))
                    self.worker_tids.add(threading.get_native_id())
                return original(worker, schedule)
            NPUWorker.execute_model = counted
            self.llm = LLM(model=str(ROOT / "model-config/Qwen3-1.7B"), load_format="dummy",
                           dtype="bfloat16", tensor_parallel_size=1, seed=0, enforce_eager=True,
                           max_num_seqs=1, block_size=128, max_model_len=2048,
                           max_num_batched_tokens=2048, gpu_memory_utilization=.1,
                           enable_prefix_caching=False, enable_chunked_prefill=False,
                           async_scheduling=False, skip_tokenizer_init=True,
                           generation_config="vllm", disable_log_stats=True,
                           compilation_config={"mode": 0, "cudagraph_mode": "NONE"})
            cc = self.llm.llm_engine.vllm_config.compilation_config
            if getattr(cc.mode, "value", cc.mode) != 0 or getattr(cc.cudagraph_mode, "name", str(cc.cudagraph_mode)) != "NONE":
                raise RuntimeError("不是纯eager")
            self.prompt = [{"prompt_token_ids": [1, 9707, 11, 382, 100]}]

    def generate(self, tokens):
            self.steps.clear()
            self.worker_tids.clear()
            self.active = True
            begin = time.monotonic_ns()
            try:
                result = self.llm.generate(self.prompt, self.SamplingParams(
                    temperature=0, max_tokens=tokens, ignore_eos=True, detokenize=False), use_tqdm=False)
                self.torch.npu.synchronize()
            finally:
                self.active = False
            end = time.monotonic_ns()
            output = list(result[0].outputs[0].token_ids)
            if len(output) != tokens or len(self.steps) != tokens or self.worker_tids != {threading.get_native_id()}:
                raise RuntimeError("输出、真实worker步数或worker线程身份错误")
            digest = hashlib.sha256(json.dumps(output).encode()).hexdigest()
            if tokens == 1002 and hasattr(self, "full_sha") and digest != self.full_sha:
                raise RuntimeError("完整输出SHA变化")
            if tokens == 50 and hasattr(self, "warm_sha") and digest != self.warm_sha:
                raise RuntimeError("settle输出SHA变化")
            return {"tokens": tokens, "output_sha256": digest, "actual_worker_steps": len(self.steps),
                    "worker_tids": sorted(self.worker_tids), "wall_ns": end - begin,
                    "business_start_monotonic_ns": begin, "business_end_monotonic_ns": end,
                    "llm_identity": id(self.llm), "scheduled_tokens": list(self.steps)}

    def command(self,cmd):
        if cmd['op']=='measure':
            before=affinities();assert_affinities(before)
            settle=self.generate(50)
            before=affinities();assert_affinities(before)
            with self.perf.region('complete-generate-and-final-synchronize') as counters:
                result=self.generate(1002)
            after=affinities();assert_affinities(after)
            if {r['tid'] for r in before}!={r['tid'] for r in after}:raise RuntimeError('ROI出现新线程')
            for event in counters['events'].values():
                if event['enabled']<=0 or event['running']/event['enabled']<.999:raise RuntimeError('PMU无效')
            result.update(events=counters['events'],settle_sha256=settle['output_sha256'],
                thread_affinities_before_raw=before,thread_affinities_after_raw=after,
                source_sha256=self.sources())
            return result
        if cmd['op']=='maps':return {'maps':Path('/proc/self/maps').read_text()}
        if cmd['op']=='python-gate':
            from python_gate import run
            return run(self)
        if cmd['op']=='reserve-full':
            from online import reserve
            return reserve(self,cmd)
        if cmd['op']=='load-full':
            from online import load
            return load(self,cmd)
        if cmd['op']=='exit':
            self.perf.close();return {'exited':True}
        raise ValueError(cmd['op'])

def main():
    p=argparse.ArgumentParser();p.add_argument('--arm',required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    os.mkfifo(a.out/'control.fifo',0o600)
    try:
        fd=os.open(a.out/'control.fifo',os.O_RDWR)
        try:
            worker=Worker(a.arm,a.out)
            callback_type=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t)
            def handle(pointer,length):
                cmd=None
                try:
                    cmd=json.loads(ctypes.string_at(pointer,length));result=worker.command(cmd)
                except Exception as error:
                    details={'status':'error','error':repr(error),'traceback':traceback.format_exc()}
                    if cmd and cmd['op'] in ('reserve-full','load-full'):
                        atomic_json(a.out/('done-'+cmd['id']+'.json'),details);return 1
                    atomic_json(a.out/'error.json',details);return -1
                atomic_json(a.out/('done-'+cmd['id']+'.json'),{'status':'ok','result':result})
                return 0 if cmd['op']=='exit' else 1
            callback=callback_type(handle)
            worker.helper.rb_fifo_loop.argtypes=[ctypes.c_int,callback_type]
            if worker.helper.rb_fifo_loop(fd,callback)<0:raise RuntimeError('C层请求gate退出失败')
        finally:os.close(fd)
    except BaseException as error:
        atomic_json(a.out/'error.json',{'error':repr(error),'traceback':traceback.format_exc()});raise
if __name__=='__main__':main()
