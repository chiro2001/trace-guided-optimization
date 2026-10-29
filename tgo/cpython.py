"""Rebuild a matching CPython with workload ROI GCC profiles and LTO."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

from .artifact import stage_image
from .collect import record
from .capture_set import merge_captures,fingerprint,workload_contract
from .elf import ELF
from .runtime import build_runtime,make_launcher
from .util import read_json,write_json,sha256,run,tool,tool_env,workload_env,IdentityError
from .validate import execute_trial


def source_archive(version,args):
    project=Path(__file__).resolve().parents[1]
    lock_root=Path(os.environ['TGO_APPDIR']) if os.environ.get('TGO_APPDIR') else project
    source_kind = 'python' if getattr(args,'python_source',None) and str(args.python_source).endswith('.xz') else 'python-gzip'
    locked=read_json(lock_root/'dependencies.lock.json')[source_kind]
    if locked['version']!=version:
        raise ValueError(f'CPython {version} is not in the source lock; do not substitute a different version')
    name=locked['url'].rsplit('/',1)[-1]
    explicit=getattr(args,'python_source',None)
    cached=project/'deps/downloads'/name
    if explicit:path=Path(explicit).resolve()
    elif cached.exists():path=cached
    else:
        cache=Path(os.environ.get('TGO_SOURCE_CACHE',str(Path.home()/'.cache/tgo/sources')))
        cache.mkdir(parents=True,exist_ok=True);path=cache/name
        if not path.exists():
            temporary=path.with_suffix(path.suffix+'.part')
            with urllib.request.urlopen(locked['url'],timeout=60) as response,temporary.open('wb') as stream:
                shutil.copyfileobj(response,stream,1024*1024)
            if sha256(temporary)!=locked['sha256']:raise IdentityError('downloaded CPython source SHA differs from lock')
            temporary.replace(path)
    if sha256(path)!=locked['sha256']:raise IdentityError('CPython source archive SHA differs from lock')
    return path,locked


def extract(archive,directory):
    directory.mkdir()
    # The locked archive was verified before this call. Minimal packaged
    # Python builds need not include _lzma; use the build host's tar/xz tools
    # in that case, alongside its already-required GCC and make.
    try:
        import lzma
        python_xz = True
    except ImportError:
        python_xz = False
    if python_xz or not str(archive).endswith('.xz'):
        with tarfile.open(archive) as handle:
            handle.extractall(directory,filter='data')
    else:
        subprocess.run([tool('tar'),'--extract','--file',str(archive),'--directory',str(directory),
                        '--no-same-owner','--no-same-permissions'],env=tool_env(),check=True,timeout=120)
    roots=list(directory.iterdir())
    if len(roots)!=1 or not (roots[0]/'configure').is_file():raise ValueError('unexpected CPython source archive')
    return roots[0]


def configuration(tree,prefix,lto=False):
    hooks=['-Wl,-u,__gcov_reset','-Wl,-u,__gcov_dump',
           '-Wl,--export-dynamic-symbol=__gcov_reset','-Wl,--export-dynamic-symbol=__gcov_dump']
    return [str(tree/'configure'),'--prefix='+prefix,'--enable-shared','--enable-optimizations','--without-ensurepip',
            *(['--with-lto'] if lto else []),'CFLAGS=-O3 -g -fno-semantic-interposition',
            'LDFLAGS='+' '.join(['-Wl,-rpath,'+str(tree),'-Wl,-q',*hooks]),'LIBS=-ldl -lm -lgcov']


def build_cpython(args,original,state,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    env=workload_env();env.update(state['env'])
    info=json.loads(subprocess.check_output([state['command'][0],'-c',
        'import json,sys,sysconfig;print(json.dumps({"version":sys.version.split()[0],"prefix":sys.base_prefix,"soabi":sysconfig.get_config_var("SOABI"),"compiler":sysconfig.get_config_var("CC")}))'],
        env=env,cwd=state['cwd'],text=True))
    original_meta=ELF(original).metadata()
    archive,locked=source_archive(info['version'],args)
    compiler=tool('gcc')
    buildenv=tool_env();buildenv.update(CC=compiler,CXX=tool('g++'))
    jobs=str(getattr(args,'build_jobs',8));timeout=getattr(args,'build_timeout',3600)
    manifest={'schema':'tgo.cpython-build.v1','state':'configuring-instrumentation','interpreter':info,
        'original':original_meta,'contract':workload_contract(state),'source':locked,
        'source_archive':str(archive),'gcc':{'path':compiler,'sha256':sha256(compiler),
        'version':subprocess.check_output([compiler,'--version'],env=buildenv,text=True).splitlines()[0]},
        'commands':[],'training_trials':[]}
    path=directory/'build.json'
    def save():write_json(path,manifest)
    def step(command,label,cwd):
        result=run(command,directory/(label+'.log'),cwd=cwd,env=buildenv,timeout=timeout)
        manifest['commands'].append(result);save();return result
    save()
    instrumented=extract(archive,directory/'instrumentation-source')
    step(configuration(instrumented,info['prefix']),'configure-instrumentation',instrumented)
    step(['make','-j'+jobs,'profile-gen-stamp'],'build-instrumentation',instrumented)
    stale=list(instrumented.rglob('*.gcda'))
    for profile in stale:profile.unlink()
    manifest['removed_build_profiles']=len(stale)
    library=instrumented/Path(original).name
    image=stage_image(original,library,directory/'instrumented-image')
    runtime=build_runtime(directory/'runtime')
    hooks=directory/'gcc-hooks.json';roi_log=directory/'gcc-rois.jsonl'
    write_json(hooks,{'kind':'gcc','library':image['optimized'],'sha256':image['output']['sha256'],'log':str(roi_log)})
    deployed=dict(state['env']);deployed['TGO_TRAINING_HOOKS']=str(hooks)
    entry=make_launcher(runtime,runtime/'train',state['command'],[image],deployed,state['cwd'])
    manifest.update(state='training',instrumented=image);save()
    for index in range(getattr(args,'train_runs',3)):
        prefix=directory/f'training-{index:03d}'
        trial_env=dict(env);trial_env['TGO_RESULT_FILE']=str(prefix)+'.result.json'
        code,seconds,timed_out,digest=execute_trial([entry],prefix,trial_env,state['cwd'],args.timeout)
        manifest['training_trials'].append({'returncode':code,'seconds':seconds,'timed_out':timed_out,
            'stdout_sha256':digest,'evidence_prefix':str(prefix)})
        save()
        if code or timed_out:raise ValueError(f'compiler ROI training failed; inspect {prefix}.stderr')
        result=Path(str(prefix)+'.result.json')
        if result.exists():
            digest=hashlib.sha256(json.dumps(read_json(result),sort_keys=True,allow_nan=False).encode()).hexdigest()
            expected=state.get('correctness_sha256') or manifest.get('training_correctness_sha256')
            if expected and expected!=digest:raise ValueError('PGO instrumentation changed workload results')
            manifest['training_correctness_sha256']=digest
    rois=[json.loads(line) for line in roi_log.read_text().splitlines()] if roi_log.exists() else []
    if not rois:raise ValueError('GCC training did not observe any explicit ROI')
    profiles=sorted(instrumented.rglob('*.gcda'))
    critical=['Python/ceval.gcda','Objects/call.gcda','Objects/dictobject.gcda','Objects/typeobject.gcda']
    if not all((instrumented/name).is_file() for name in critical):raise ValueError('critical interpreter profiles missing')
    manifest.update(state='building-pgo-lto',completed_rois=len(rois),
        profiles=[{'path':str(p.relative_to(instrumented)),'sha256':sha256(p)} for p in profiles]);save()
    optimized=extract(archive,directory/'optimized-source')
    step(configuration(optimized,info['prefix'],True),'configure-pgo-lto',optimized)
    for profile in profiles:
        target=optimized/profile.relative_to(instrumented);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(profile,target)
    step(['make','-j'+jobs,'build_all','CFLAGS_NODIST=-fprofile-use -fprofile-correction','LDFLAGS_NODIST='],
         'build-pgo-lto',optimized)
    output=optimized/Path(original).name
    if not ELF(output).metadata()['relocations']:raise ValueError('PGO/LTO Python did not retain text relocations')
    image=stage_image(original,output,directory/'optimized-image')
    manifest.update(state='complete',output=image,training_runs_completed=len(manifest['training_trials']));save()
    return image,manifest


def tune_cpython(args,source):
    source=Path(source).resolve();state=read_json(source/'run.json');profile=read_json(source/'profile.json')
    candidates=[r for r in profile['images'] if Path(r['path']).name.startswith('libpython')]
    if len(candidates)!=1:raise ValueError('CPython adapter needs exactly one identified libpython')
    original=candidates[0]['path']
    root=Path(args.output).absolute();root.mkdir(parents=True,exist_ok=False)
    result={'schema':'tgo.tune.v1','strategy':'cpython-pgo-lto','state':'building','selected':'baseline','baseline_capture':str(source)}
    write_json(root/'tune.json',result)
    try:
        image,compiler=build_cpython(args,original,state,root/'compiler')
        captures=[];sampler=None
        for index in range(getattr(args,'train_runs',3)):
            recording=deepcopy(args);recording.command=state['command'];recording.env=[f'{k}={v}' for k,v in state['env'].items()]
            recording.output=str(root/f'training/capture-{index:03d}')
            if sampler:recording.backend=sampler['backend'];recording.event=sampler['event']
            record(recording,[image]);captures.append(Path(recording.output))
            sampler=fingerprint(read_json(captures[-1]/'run.json'))
            result['training_runs_completed']=len(captures);write_json(root/'tune.json',result)
        merged=root/'capture';merge_captures(captures,merged)
        optimize=deepcopy(args);optimize.run=str(merged);optimize.output=str(root/'layouts');optimize.strategy='bolt'
        optimize.entry=args.entry or str(root/'run')
        from .tune import tune
        result=tune(optimize)
        result.update(strategy='cpython-pgo-lto',cpython_build=str(root/'compiler/build.json'),
                      cpython_build_sha256=sha256(root/'compiler/build.json'),baseline_capture=str(source))
        build_runtime(root)
        write_json(root/'build.json',read_json(root/'layouts/build.json'))
    except BaseException as error:
        result.update(state='failed',error=str(error));raise
    finally:write_json(root/'tune.json',result)
    return result
