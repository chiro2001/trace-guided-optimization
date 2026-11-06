"""持久任务：argv启动、独立进程组、身份核验取消、原始日志保留。"""
import argparse
import ctypes
import json
import math
import os
import platform
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
from .util import write_json,read_json,terminate_process_group,sha256,tool,tool_env_key,dependency_asset

SCHEMA='tgo.job.v1'

def starttime(pid):
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19])

def memory_available_gib():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):return int(line.split()[1])/(1024**2)
    raise RuntimeError('找不到MemAvailable')

def proc_identity(pid):
    folder=Path(f'/proc/{pid}');fields=(folder/'stat').read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'ppid':int(fields[1]),'starttime':int(fields[19]),'uid':folder.stat().st_uid,'state':fields[0]}

def descendants(pid):
    children={};rows={}
    for folder in Path('/proc').iterdir():
        if not folder.name.isdigit():continue
        try:row=proc_identity(int(folder.name))
        except (OSError,ValueError,IndexError):continue
        rows[row['pid']]=row;children.setdefault(row['ppid'],[]).append(row['pid'])
    pending=list(children.get(pid,[]));found=[]
    while pending:
        child=pending.pop();pending.extend(children.get(child,[]))
        if rows[child]['uid']==os.getuid():found.append(rows[child])
    return found

def same_live_process(row):
    try:actual=proc_identity(row['pid'])
    except (OSError,ValueError,IndexError):return False
    return actual['starttime']==row['starttime'] and actual['uid']==row['uid'] and actual['state']!='Z'

def terminate_descendants(rows,grace=5):
    for row in rows:
        if same_live_process(row):
            try:os.kill(row['pid'],signal.SIGTERM)
            except ProcessLookupError:pass
    deadline=time.monotonic()+grace
    while time.monotonic()<deadline and any(same_live_process(row) for row in rows):time.sleep(.05)
    for row in rows:
        if same_live_process(row):
            try:os.kill(row['pid'],signal.SIGKILL)
            except ProcessLookupError:pass
    return len(rows)

def state(directory):
    root=Path(directory).resolve();path=root/'job.json'
    result=read_json(path)
    if result.get('schema')!=SCHEMA:raise ValueError('不是TGO任务目录')
    return result

def freeze_command(command,directory,project=None):
    """TGO任务使用源码副本；其它程序的argv保持调用者定义。"""
    marker=next((i for i in range(len(command)-1) if command[i:i+2]==['-m','tgo']),None)
    if marker is None:return list(command),None
    project=Path(project or Path(__file__).resolve().parents[1]).resolve()
    snapshot=Path(directory)/'source';snapshot.mkdir()
    for folder in ['tgo','patches','benchmarks']:
        if (project/folder).is_dir():shutil.copytree(project/folder,snapshot/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    if (project/'dependencies.lock.json').is_file():shutil.copy2(project/'dependencies.lock.json',snapshot/'dependencies.lock.json')
    entry=snapshot/'_tgo_entry.py';entry.write_text('from tgo.cli import main\nraise SystemExit(main())\n')
    frozen=list(command[:marker])+[str(entry)]+list(command[marker+2:])
    for i,arg in enumerate(frozen):
        for folder in ['tgo/workloads','benchmarks']:
            prefix=str(project/folder)+'/'
            if arg.startswith(prefix) and (snapshot/arg[len(str(project))+1:]).is_file():
                frozen[i]=str(snapshot/arg[len(str(project))+1:])
    files={str(p.relative_to(snapshot)):sha256(p) for p in snapshot.rglob('*') if p.is_file()}
    write_json(Path(directory)/'source-snapshot.json',{'source':str(project),'files':files,
        'purpose':'固定本次执行源码；不是应用ELF资格白名单'})
    return frozen,{'directory':str(snapshot),'files':len(files),'manifest':str(Path(directory)/'source-snapshot.json')}

def frozen_tool_environment(overrides=None,project=None):
    """在源码复制之前解析私有工具，避免副本的deps路径消失。仅固化工具路径。"""
    project=Path(project or Path(__file__).resolve().parents[1]).resolve()
    explicit=dict(overrides or {});environment={**os.environ,**explicit};resolved={}
    for name in ['cc','gcc','g++','clang','taskset','tar','go','perf','llvm-bolt','llvm-bolt-recover',
                 'llvm-bolt-live','perf2bolt','merge-fdata','patchelf','readelf','objdump','objcopy',
                 'aarch64-linux-gnu-objcopy','qemu-'+platform.machine()]:
        key=tool_env_key(name)
        path=tool(name,required=False,environment=environment,project=project)
        if path and key not in environment:resolved[key]=path
    for key,candidates in {
        'TGO_QEMU_HEADER':[dependency_asset('include/qemu-plugin.h',environment=environment,project=project)],
        'TGO_BOLT_RUNTIME':[dependency_asset('lib/libbolt_rt_instr.a',environment=environment,project=project),project/'deps/build/llvm/lib/libbolt_rt_instr.a']}.items():
        if key not in environment:
            path=next((Path(p) for p in candidates if p and Path(p).is_file()),None)
            if path:resolved[key]=str(path)
    return {**resolved,**explicit},resolved

def launch(command,directory,cwd=None,env=None,timeout=None,snapshot=True,mem_floor_gib=None,wait_for=None):
    if not command or not all(isinstance(a,str) for a in command):raise ValueError('任务需要非空argv字符串列表')
    if timeout is not None and (not math.isfinite(timeout) or timeout<=0):raise ValueError('timeout必须为有限正数')
    if mem_floor_gib is not None and (not math.isfinite(mem_floor_gib) or mem_floor_gib<=0):raise ValueError('mem-floor-gib必须为有限正数')
    root=Path(directory).resolve()
    dependencies=list(dict.fromkeys(str(Path(path).resolve()) for path in (wait_for or [])))
    if str(root) in dependencies:raise ValueError('任务不能等待自身')
    for dependency in dependencies:state(dependency)
    root.mkdir(parents=True,exist_ok=False,mode=0o700)
    execution,source=freeze_command(command,root) if snapshot else (list(command),None)
    task_env,resolved=frozen_tool_environment(env) if source else (dict(env or {}),{})
    spec={'schema':SCHEMA,'command':command,'cwd':str(Path(cwd or os.getcwd()).resolve()),
        'execution_command':execution,'source_snapshot':source,
        'env':task_env,'resolved_tools':resolved,'timeout':timeout,'mem_floor_gib':mem_floor_gib,'wait_for':dependencies,'created_unix':time.time()}
    write_json(root/'spec.json',spec);(root/'spec.json').chmod(0o600)
    package_root=str(Path(__file__).resolve().parents[1]);control_env=os.environ.copy()
    control_env['TGO_JOB_PARENT_PYTHONPATH']=json.dumps(control_env.get('PYTHONPATH'))
    control_env['PYTHONPATH']=package_root+(os.pathsep+control_env['PYTHONPATH'] if control_env.get('PYTHONPATH') else '')
    with (root/'supervisor.log').open('xb') as log:
        process=subprocess.Popen([sys.executable,'-m','tgo.jobs','_run',str(root)],
            cwd=package_root,env=control_env,stdout=log,stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,start_new_session=True)
    threading.Thread(target=process.wait,daemon=True).start()
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if (root/'job.json').exists():return state(root)
        if process.poll() is not None:raise RuntimeError('任务监督器启动失败，见'+str(root/'supervisor.log'))
        time.sleep(.02)
    raise RuntimeError('任务监督器尚未报告状态，见'+str(root/'supervisor.log'))

def stop(directory):
    root=Path(directory).resolve();record=state(root)
    if record['state'] in ('complete','failed','cancelled','timed_out','resource_limit'):return record
    pid=record['supervisor_pid']
    try:
        actual=starttime(pid)
        args=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        uid=Path(f'/proc/{pid}').stat().st_uid
    except (FileNotFoundError,ProcessLookupError):
        raise RuntimeError('监督器已不在；不向记录的子进程PID盲目发信号')
    if actual!=record['supervisor_starttime'] or b'tgo.jobs' not in args or os.fsencode(root) not in args or uid!=os.getuid():
        raise RuntimeError('任务监督器身份不符，拒绝取消')
    tree=descendants(pid)
    os.kill(pid,signal.SIGTERM)
    count=terminate_descendants(tree)
    return {'schema':SCHEMA,'directory':str(root),'state':'cancellation_requested','descendants_checked':count}

def supervise(directory):
    root=Path(directory).resolve();spec=read_json(root/'spec.json');process=None;cancel=False
    def on_stop(_signum,_frame):
        nonlocal cancel
        cancel=True
    signal.signal(signal.SIGTERM,on_stop);signal.signal(signal.SIGINT,on_stop)
    # 独立setsid的后代在父进程退出后也交给本监督器回收。
    subreaper=ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)==0
    record={'schema':SCHEMA,'directory':str(root),'state':'starting','supervisor_pid':os.getpid(),
        'supervisor_starttime':starttime(os.getpid()),'started_unix':time.time(),'command':spec['command'],
        'cwd':spec['cwd'],'explicit_env_keys':sorted(spec['env'])}
    record['source_snapshot']=spec.get('source_snapshot')
    record['subreaper']=subreaper
    write_json(root/'job.json',record)
    start=time.monotonic();execution_start=None
    try:
        last_waiting=None
        while True:
            waiting=[path for path in spec.get('wait_for',[]) if state(path)['state'] not in ('complete','failed','cancelled','timed_out','resource_limit')]
            if not waiting:break
            if waiting!=last_waiting:
                record.update(state='waiting',waiting_for=waiting);write_json(root/'job.json',record);last_waiting=waiting
            if cancel:record['state']='cancelled';return record
            if spec['timeout'] is not None and time.monotonic()-start>spec['timeout']:
                record['state']='timed_out';return record
            time.sleep(.2)
        floor=spec.get('mem_floor_gib')
        record.pop('waiting_for',None)
        record['mem_floor_gib']=floor
        if floor:
            record['min_mem_available_gib']=memory_available_gib()
            if record['min_mem_available_gib']<floor:
                record.update(state='resource_limit',error='启动前可用内存低于阈值');return record
        env=os.environ.copy();env.update(spec['env'])
        original_pythonpath=json.loads(env.pop('TGO_JOB_PARENT_PYTHONPATH','null'))
        if 'PYTHONPATH' not in spec['env']:
            if original_pythonpath is None:env.pop('PYTHONPATH',None)
            else:env['PYTHONPATH']=original_pythonpath
        with (root/'stdout.log').open('xb') as stdout,(root/'stderr.log').open('xb') as stderr:
            process=subprocess.Popen(spec.get('execution_command',spec['command']),cwd=spec['cwd'],env=env,stdin=subprocess.DEVNULL,
                stdout=stdout,stderr=stderr,start_new_session=True)
            execution_start=time.monotonic();last_heartbeat=execution_start
            record.update(state='running',child_pid=process.pid,child_starttime=starttime(process.pid),
                execution_started_unix=time.time(),queue_elapsed_s=execution_start-start,heartbeat_unix=time.time())
            write_json(root/'job.json',record)
            while process.poll() is None:
                expired=spec['timeout'] is not None and time.monotonic()-start>spec['timeout']
                low_memory=False
                if floor:
                    available=memory_available_gib();record['min_mem_available_gib']=min(record['min_mem_available_gib'],available)
                    low_memory=available<floor
                if cancel or expired or low_memory:
                    terminate_process_group(process)
                    record['state']='cancelled' if cancel else 'timed_out' if expired else 'resource_limit';break
                if time.monotonic()-last_heartbeat>=5:
                    last_heartbeat=time.monotonic();record.update(heartbeat_unix=time.time(),execution_elapsed_s=last_heartbeat-execution_start)
                    write_json(root/'job.json',record)
                time.sleep(.05)
            else:record['state']='cancelled' if cancel else 'complete' if process.returncode==0 else 'failed'
            record['returncode']=process.returncode
    except BaseException as error:
        record.update(state='failed',error=str(error))
        if process is not None:terminate_process_group(process)
    finally:
        remaining=descendants(os.getpid())
        if remaining:record['descendants_cleaned']=terminate_descendants(remaining)
        if subreaper:
            while True:
                try:
                    if os.waitpid(-1,os.WNOHANG)[0]==0:break
                except ChildProcessError:break
        if execution_start is not None:record['execution_elapsed_s']=time.monotonic()-execution_start
        record.update(finished_unix=time.time(),heartbeat_unix=time.time(),elapsed_s=time.monotonic()-start);write_json(root/'job.json',record)
    return record

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['_run']);parser.add_argument('directory')
    args=parser.parse_args();supervise(args.directory)
if __name__=='__main__':main()
