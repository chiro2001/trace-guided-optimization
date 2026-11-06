"""自有进程、原状态、在线构建/切换/回滚；各输入身份现场采集。"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import statistics
import shutil
import subprocess
import sys
import time
import uuid
from tgo.util import write_json,read_json,sha256,run,tool,IdentityError
from .build import build
from .common import TOKEN_ENV
from .aarch64_elf import parse_maps
from .ptrace import ProcFS,Ptrace,StopWorld
from .transaction import patch
from .common import profile_function,profile_score

def require_platform():
    if platform.machine()!='aarch64' or os.sysconf('SC_PAGE_SIZE')!=4096:
        raise ValueError('当前在线后端需要原生Linux AArch64与4KiB页')

class Session:
    def __init__(self,output,configuration,python=None):
        require_platform()
        self.out=Path(output).resolve();self.out.mkdir(parents=True,exist_ok=False,mode=0o700)
        self.workspace=self.out;self.config={**configuration,'controller_pid':os.getpid()}
        self.python=python or sys.executable;self.token=secrets.token_hex(24);self.process=None;self.pid=None
        self.record={'schema':'tgo.live-session.v1','state':'initializing','configuration':configuration,
            'results':[],'known_input_allowlist':False,'started_unix':time.time(),'controller_python_version':sys.version}
        self.loaded=[];self.applied=False;self.save()

    def save(self):write_json(self.out/'session.json',self.record)

    def identity(self):
        proc=ProcFS()
        if proc.starttime(self.pid)!=self.starttime:raise IdentityError('在线目标PID已复用')
        if int(proc.status(self.pid)['Uid'].split()[0])!=os.getuid():raise IdentityError('在线目标UID不符')
        if proc.environment(self.pid).get(TOKEN_ENV.encode())!=self.token.encode():raise IdentityError('在线会话nonce不符')

    def wait(self,path,timeout=300):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            error=self.out/'worker/error.json'
            if error.exists():raise RuntimeError(str(read_json(error)))
            if Path(path).is_file():return read_json(path)
            if self.process is not None and self.process.poll() is not None:raise RuntimeError('在线worker提前退出，见worker.log')
            time.sleep(.01)
        raise TimeoutError('在线操作超时:'+str(path))

    def launch(self):
        runtime=self.out/'artifacts/runtime';runtime.mkdir(parents=True)
        worker_source=runtime/'source';worker_source.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1],worker_source/'tgo',
            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        worker_files={str(path.relative_to(worker_source)):sha256(path) for path in worker_source.rglob('*') if path.is_file()}
        write_json(runtime/'worker-source.json',{'schema':'tgo.live-worker-source.v1','files':worker_files,
            'purpose':'固定纯Python worker源码；避免向另一版本解释器注入控制器整个site-packages'})
        source=Path(__file__).parent/'native/loader.c';self.helper=runtime/'libtgo_live.so'
        result=run([tool('cc'),'-O2','-g','-shared','-fPIC',source,'-o',self.helper,'-lgcc_s'],runtime/'build.log')
        self.record['runtime']={'helper':str(self.helper),'sha256':sha256(self.helper),'source_sha256':sha256(source),'build':result,
            'worker_source':str(worker_source),'worker_source_manifest_sha256':sha256(runtime/'worker-source.json')};self.save()
        config=self.out/'worker-config.json';write_json(config,self.config)
        env=os.environ.copy();env[TOKEN_ENV]=self.token;env['TGO_LIVE_WORKSPACE']=str(self.out)
        env['PYTHONPATH']=str(worker_source)
        self.log=(self.out/'worker.log').open('xb')
        self.process=subprocess.Popen([self.python,'-m','tgo.live.worker','--workspace',str(self.out),
            '--config',str(config),'--helper',str(self.helper)],cwd=self.out,env=env,stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        self.pid=self.process.pid;self.starttime=ProcFS().starttime(self.pid)
        self.record.update(pid=self.pid,starttime_ticks=self.starttime);self.save()
        self.ready=self.wait(self.out/'worker/ready.json',120);self.identity()
        if self.ready['pid']!=self.pid or self.ready['starttime_ticks']!=self.starttime:raise IdentityError('worker就绪身份不一致')
        self.record.update(state='ready',ready=self.ready);self.save()

    def rpc(self,operation,timeout=300,**kwargs):
        return self.receive(self.send(operation,**kwargs),timeout)

    def send(self,operation,**kwargs):
        self.identity();ident=uuid.uuid4().hex
        payload=(json.dumps({'id':ident,'token':self.token,'op':operation,**kwargs})+'\n').encode()
        deadline=time.monotonic()+10;fd=None
        while time.monotonic()<deadline:
            try:fd=os.open(self.out/'worker/control.fifo',os.O_WRONLY|os.O_NONBLOCK);break
            except OSError as error:
                if error.errno not in (errno.ENXIO,errno.ENOENT):raise
                if self.process.poll() is not None:raise RuntimeError('worker未能进入请求gate')
                time.sleep(.01)
        if fd is None:raise TimeoutError('请求gate打开超时')
        try:
            if len(payload)>4096:raise ValueError('在线控制请求超过原子FIFO写入限制')
            if os.write(fd,payload)!=len(payload):raise RuntimeError('控制请求未完整写入')
        finally:os.close(fd)
        return ident

    def receive(self,ident,timeout=300):
        response=self.wait(self.out/'worker'/('done-'+ident+'.json'),timeout)
        if response['status']!='ok':raise RuntimeError(str(response))
        return response['result']

    def normalize(self):
        self.identity();proc=ProcFS();ptrace=Ptrace();events=[]
        target=str(Path(self.config['image']).resolve());started=time.monotonic_ns()
        with StopWorld({'pid':self.pid,'starttime_ticks':self.starttime},proc,ptrace,events):
            self.identity();regions=[r for r in parse_maps(proc.maps(self.pid).decode()) if r['path']==target and 'x' in r['perms']]
            if not regions:raise RuntimeError('找不到原始执行映射')
            fd=os.open(f'/proc/{self.pid}/mem',os.O_RDONLY);words=[];before=hashlib.sha256()
            try:
                for region in regions:
                    data=os.pread(fd,region['end']-region['start'],region['start'])
                    if len(data)!=region['end']-region['start']:raise RuntimeError('原执行区域读取不足')
                    before.update(data)
                    for address in range(region['start'],region['end'],4096):words.append((address,os.pread(fd,8,address)))
                journal={'schema':'tgo.live-cow.v1','pid':self.pid,'starttime_ticks':self.starttime,'words':[{'address':a,'old':raw.hex()} for a,raw in words]}
                path=self.out/'cow-journal.json'
                with path.open('x') as stream:json.dump(journal,stream);stream.flush();os.fsync(stream.fileno())
                for address,raw in words:
                    if len(raw)!=8:raise RuntimeError('COW原值读取不足')
                    ptrace.request(4,self.pid,address,int.from_bytes(raw,'little'))
                after=hashlib.sha256()
                for region in regions:after.update(os.pread(fd,region['end']-region['start'],region['start']))
                if before.digest()!=after.digest():raise RuntimeError('COW归一改变了原执行内容')
            finally:os.close(fd)
        result={'pages':len(words),'old_execution_sha256':before.hexdigest(),'code_bytes_unchanged':True,
            'elapsed_ns':time.monotonic_ns()-started,'events':events}
        self.record['cow_normalization']=result;self.save();return result

    def prepare(self,reference,bolt=None,lite=False,function_order=None,capsule=False,layout='ext-tsp',optimization_level='aggressive'):
        name=Path(reference['source']).name
        loaded=self.ready['libraries'].get(name)
        if not loaded or loaded['sha256']!=reference['source_sha256']:raise IdentityError('工作进程映像与画像输入不一致')
        size=32<<20 if self.ready['structure']['runtime']=='cpython' else 16<<20
        reservation=self.rpc('reserve',size=size);self.record['reservation']=reservation;self.save()
        folder=self.out/'artifacts'/name
        report=build(reference,folder,reservation['custom_vma'],bolt,lite,function_order,capsule,layout,optimization_level)
        self.record['build']={k:report['build'][k] for k in ['source_sha256','fdata_sha256','tool_sha256','command','runtime',
            'result','started_monotonic_ns','finished_monotonic_ns','normalization','online_capsule','standalone_executable',
            'layout','optimization_level','lite']};self.save()
        self.record['build'].update(original_function_count=report['original_function_count'],
            generated_function_count=report['function_count'],unmapped_function_count=len(report['rejected']),
            inspection_summary=str(folder/'summary.json'));self.save()
        package=self.rpc('load',folder=str(folder),output_sha256=report['output_sha256'])
        self.loaded=[package]
        self.reference=reference
        self.record['loaded']={k:v for k,v in package.items() if k not in ('patches','new_regions','frame_checks','entry_rejections')}
        self.record['loaded']['entry_rejection_count']=len(package['entry_rejections']);self.save()
        if not package['function_count']:raise RuntimeError('没有任何入口通过在线资格')
        return package

    def measure(self,state,requests=1):
        for index in range(requests):
            result=self.rpc('measure');self.record['results'].append({'state':state,'index':index,**result});self.save()
        return self.record['results'][-requests:]

    def switch(self,apply):
        if self.applied==apply:raise ValueError('在线入口已经处于所请求状态')
        operation='apply' if apply else 'rollback'
        number=len(self.record.get('transitions',[]))
        result=patch(self,self.loaded,apply,f'{number:04d}-{operation}')
        self.applied=apply;self.record['apply' if apply else 'rollback']=result;self.save()
        self.record.setdefault('transitions',[]).append({'operation':operation,**result});self.save()
        self.record.setdefault('semantic_gates',[]).append(self.rpc('semantic'));self.save()
        return result

    def prove(self):
        scores={}
        for line in Path(self.reference['fdata_path']).read_text().splitlines():
            fields=line.split()
            if len(fields)==8 and fields[3]=='1' and fields[5]=='0':
                key=profile_function(fields[4]);scores[key]=scores.get(key,0)+int(fields[7])
        library=self.loaded[0]
        choices=[(profile_score(scores,p['name'],p['address']-library['load_bias']),p) for p in library['patches']]
        score,target=max(choices,key=lambda pair:pair[0])
        if score<=0:raise RuntimeError('画像中没有可关联到新入口的热点')
        from .probe import prove
        result,proof=prove(self,target['target'])
        self.record['results'].append({'state':'execution-proof','function':target['name'],'profile_entries':score,**result});self.save()
        return proof

    def close(self):
        if self.process is None:return
        if self.process.poll() is None:
            try:
                release=os.open(self.out/'worker/probe-release.fifo',os.O_WRONLY|os.O_NONBLOCK)
                try:os.write(release,b'X')
                finally:os.close(release)
            except OSError:pass
            if self.applied:
                try:self.switch(False)
                except BaseException as error:self.record['cleanup_rollback_error']=str(error);self.save()
            try:self.rpc('exit',timeout=2)
            except BaseException:pass
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.identity();self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.identity();self.process.kill();self.process.wait(timeout=3)
        if hasattr(self,'log'):self.log.close()

def replay(reference,output,configuration,python=None,bolt=None,requests=2,lite=False,function_order=None,capsule=False):
    session=Session(output,configuration,python)
    try:
        session.launch();session.prepare(reference,bolt,lite,function_order,capsule);session.normalize()
        session.measure('baseline',requests);session.switch(True);session.prove();session.measure('online',requests)
        session.switch(False);session.measure('rollback',requests)
        values=session.record['results']
        if len({r['output_sha256'] for r in values})!=1:raise RuntimeError('在线/回滚的语义摘要不一致')
        session.rpc('exit');session.record.update(state='complete',finished_unix=time.time());session.save()
        return session.record
    except BaseException as error:
        session.record.update(state='failed',error=str(error));session.save();raise
    finally:session.close()


def paired_replay(reference,output,configuration,python=None,bolt=None,pairs=3,warmups=1,lite=False,function_order=None,capsule=False,
                  layout='ext-tsp',optimization_level='aggressive'):
    """同PID、同对象交替AB/BA；硬件探针和每次切换后的预热不计入收益。"""
    if pairs<=0 or warmups<0:raise ValueError('pairs须为正数，warmups不可为负数')
    session=Session(output,configuration,python)
    try:
        session.launch();session.prepare(reference,bolt,lite,function_order,capsule,layout,optimization_level);session.normalize()
        session.measure('qualification-baseline',1)
        session.switch(True);session.prove();session.switch(False)
        measurements=[]
        for index in range(pairs):
            pair={'index':index,'order':['baseline','online'] if index%2==0 else ['online','baseline']}
            for label in pair['order']:
                desired=label=='online'
                if session.applied!=desired:session.switch(desired)
                if warmups:session.measure('warmup-'+label,warmups)
                sample=session.measure(label,1)[0]
                sample['pair']=index;session.save();pair[label]=sample
            pair['reduction_percent']=100*(1-pair['online']['wall_ns']/pair['baseline']['wall_ns'])
            measurements.append(pair)
        if session.applied:session.switch(False)
        session.measure('rollback',1)
        if len({row['output_sha256'] for row in session.record['results']})!=1:
            raise RuntimeError('在线、预热或回滚的业务语义摘要不一致')
        gains=[row['reduction_percent'] for row in measurements]
        session.record['validation']={'schema':'tgo.live-validation.v1','equivalent':True,
            'requested_pairs':pairs,'completed_pairs':len(measurements),'pairs':measurements,
            'median_reduction_percent':statistics.median(gains),'min_reduction_percent':min(gains),
            'max_reduction_percent':max(gains),'metric':'same-process-business-wall',
            'excluded_states':['execution-proof','qualification-baseline','warmup-baseline','warmup-online','rollback'],
            'warmups_per_state':warmups,'candidate_selection':'固定画像与配置；未按该验证数据重选布局',
            'pmu_scope':'native-calling-thread-user'}
        session.rpc('exit');session.record.update(state='complete',finished_unix=time.time());session.save()
        return session.record
    except BaseException as error:
        session.record.update(state='failed',error=str(error));session.save();raise
    finally:session.close()
