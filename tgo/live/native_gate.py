"""用现场编译/采集的未知SHA ELF验证生成器、函数身份、TLS、异常和回滚。"""
from pathlib import Path
import os
import subprocess
import sys
from tgo.instrument import runtime_archive
from tgo.util import run,tool,sha256,write_json,cpu_set
from .session import replay,require_platform

def partitions(value=None):
    requested=[int(x) for x in cpu_set(value).split(',')] if value else sorted(os.sched_getaffinity(0))
    chosen=[];used=set()
    for cpu in requested:
        path=Path(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list')
        siblings={int(x) for x in cpu_set(path.read_text().strip()).split(',')} if path.is_file() else {cpu}
        if siblings&used:continue
        chosen.append(cpu);used.update(siblings)
        if value is None and len(chosen)==4:break
    if len(chosen)<2:raise ValueError('在线门禁至少需要两个不共享SMT的CPU；可显式提供--cpus')
    return chosen[0],chosen[1:]

def check(output,bolt,python=None,instrument_bolt=None,archive=None,cpus=None,capsule=False):
    require_platform();root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False)
    source_code=Path(__file__).parent/'native/fixture.cpp';source=root/'fixture.so'
    run([tool('g++'),'-O2','-g','-shared','-fPIC','-fno-omit-frame-pointer','-Wl,--emit-relocs','-Wl,-z,now',source_code,'-o',source],root/'compile.log')
    runtime=Path(archive).resolve() if archive else runtime_archive()
    if runtime is None:raise ValueError('缺少BOLT插桩运行库，请提供--runtime或TGO_BOLT_RUNTIME')
    generator=tool('llvm-bolt',instrument_bolt or bolt);instrumented=root/'instrument.so';profile=root/'native.fdata'
    run([generator,source,'-o',instrumented,'-instrument','-instrumentation-file='+str(profile),
        '-runtime-instrumentation-lib='+str(runtime),'-thread-count=4'],root/'instrument.log')
    training='''import ctypes,sys
lib=ctypes.CDLL(sys.argv[1]);lib.rb_fixture_step.argtypes=[ctypes.c_uint64];lib.rb_fixture_step.restype=ctypes.c_uint64
for value in range(10000):lib.rb_fixture_step(value)
'''
    run([python or sys.executable,'-c',training,instrumented],root/'train.log')
    if not profile.is_file() or profile.stat().st_size<100:raise RuntimeError('原生门禁画像未产生有效边计数')
    reference={'source':str(source),'source_sha256':sha256(source),'fdata_path':str(profile),
        'fdata_sha256':sha256(profile),'profile_kind':'fresh-native-gate','known_input_allowlist':False}
    main,helpers=partitions(cpus)
    result=replay(reference,root/'replay',{'adapter':'native-fixture','image':str(source),
        'main_cpu':main,'helper_cpus':helpers,'native_iterations':4000,'rounds':1},python,bolt,requests=1,capsule=capsule)
    report={'schema':'tgo.live-native-gate.v1','passed':result['state']=='complete',
        'input_sha256':reference['source_sha256'],'tool_sha256':sha256(bolt),'helper_sha256':result['runtime']['sha256'],
        'semantic_checks':sum(r['result'].get('checks',0) for r in result['results']),
        'hardware_execution_proofs':result.get('hardware_execution_proofs',[]),
        'known_input_allowlist':False,'replay_session':str(root/'replay/session.json')}
    write_json(root/'gate.json',report);return report
