#!/usr/bin/env python3
"""Run the pinned eager host-stub reproduction inside its workload container."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'sha256:a6314dddb8665748c456a4ba3aee5ffd985e366689327889b9b072d4ce18bf97'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-o', '--output', type=Path, required=True)
    parser.add_argument('--cpu', type=int, required=True, help='native decode ROI CPU')
    parser.add_argument('--cpus', help='container CPU set; must include --cpu')
    parser.add_argument('--mems', default='0-3')
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--models', type=Path, default=Path.home()/'models')
    parser.add_argument('--tool', type=Path, default=ROOT/'scripts/tgo-source.sh')
    parser.add_argument('--sudo', action='store_true', help='use sudo -n for Docker')
    parser.add_argument('--run', type=Path, help='reuse baseline discovery')
    parser.add_argument('--python-build', type=Path, help='reuse verified completed ROI PGO/LTO build')
    parser.add_argument('--library-capture', type=Path, help='reuse verified complete QEMU capture')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--build-timeout', type=int, default=3600)
    args = parser.parse_args()
    if args.cpu < 0 or min(args.jobs,args.timeout,args.build_timeout) <= 0:
        parser.error('CPU must be nonnegative; jobs and timeouts must be positive')
    output = args.output.resolve()
    if output.exists():
        parser.error('output already exists; use a new directory to retain evidence')
    models, tool = args.models.resolve(), args.tool.resolve()
    if not (models/'Qwen3.5-0.8B').is_dir():
        parser.error('model directory must contain Qwen3.5-0.8B')
    if not tool.is_file() or not os.access(tool,os.X_OK):
        parser.error('tool must be an executable source runner, AppImage, or AppRun')
    # Everything the workload or tool writes is mounted at the same absolute path.
    mounts = {ROOT:False,output.parent:False,tool.parent:False,models:True}
    env = {
        'PY':str(ROOT/'benchmarks/container_entry.sh'), 'TGO_APP':str(tool),
        'TGO_CONTAINER_ACTION':'tune', 'TGO_BUNDLE':str(output),
        'TGO_STRATEGY':'auto', 'TGO_BUILD_JOBS':str(args.jobs),
        'TGO_BUILD_TIMEOUT':str(args.build_timeout), 'TGO_TIMEOUT':str(args.timeout),
        'TGO_LAYOUT':'ext-tsp', 'TGO_IMAGE_SEARCH':'joint',
        'APPIMAGE_EXTRACT_AND_RUN':'1', 'VLLM_ENABLE_V1_MULTIPROCESSING':'0',
        'OMP_NUM_THREADS':'1', 'TASK_QUEUE_ENABLE':'1',
        'HF_HUB_OFFLINE':'1', 'TRANSFORMERS_OFFLINE':'1',
        'XDG_CACHE_HOME':str(ROOT/'build'/('cache-'+output.name)),
    }
    for name in ('run','python_build','library_capture'):
        value=getattr(args,name)
        if value:
            value=value.resolve()
            if not value.is_dir():parser.error(name+' directory does not exist')
            mounts.setdefault(value,False)
            env['TGO_'+name.upper()]=str(value)
    command = ['sudo','-n'] if args.sudo else []
    command += ['docker','run','--rm','--pid','host','--network','none',
        '--cpuset-cpus',args.cpus or str(args.cpu),'--cpuset-mems',args.mems,
        '--memory','40g','--memory-swap','40g','--pids-limit','2048',
        '--cap-add','SYS_ADMIN','--cap-add','IPC_LOCK','--security-opt','seccomp=unconfined']
    for path,readonly in mounts.items():
        if path==models:continue
        command += ['-v',str(path)+':'+str(path)+(':ro' if readonly else '')]
    command += ['-v',str(models)+':/models:ro']
    for name,value in env.items():command += ['-e',name+'='+value]
    command += ['--entrypoint','/bin/bash',IMAGE,'/opt/va26-scripts/run_real_test_official.sh',
        str(ROOT/'benchmarks/vllm_decode.py'),'--requests','9','--warmup-requests','1',
        '--roi-cpu',str(args.cpu),'--max-tokens','8','--no-detokenize','--check-native-ops']
    print(shlex.join(command),flush=True)
    return subprocess.call(command,cwd=ROOT)


if __name__=='__main__':raise SystemExit(main())
