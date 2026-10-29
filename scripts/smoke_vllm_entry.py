#!/usr/bin/env python3
"""Check the adjacent Python ELF in new containers with only its bundle mounted."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
IMAGE='sha256:a6314dddb8665748c456a4ba3aee5ffd985e366689327889b9b072d4ce18bf97'
ADJACENT='/usr/local/python3.12.13/bin/python3.tgo'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path)
    parser.add_argument('-o','--output',type=Path,required=True)
    parser.add_argument('--cpu',type=int,required=True)
    parser.add_argument('--cpus')
    parser.add_argument('--mems',default='0-3')
    parser.add_argument('--models',type=Path,default=Path.home()/'models')
    parser.add_argument('--sudo',action='store_true')
    parser.add_argument('--expect-images',type=int,default=8)
    args=parser.parse_args()
    bundle=args.bundle.resolve();output=args.output.resolve();models=args.models.resolve()
    build=json.loads((bundle/'build.json').read_text())
    expected={r['optimized'] for r in build['images']}
    if len(expected)!=args.expect_images:parser.error('unexpected number of optimized images')
    if any(not Path(p).is_relative_to(bundle) for p in expected):
        parser.error('all optimized images must be inside the mounted bundle')
    if build.get('entry_mode')!='executable':parser.error('entry must use executable mode')
    output.mkdir(parents=True,exist_ok=False)
    code=(ROOT/'benchmarks/vllm_decode.py').read_text()
    checks=('    import tgo.roi as portable_roi\n'
        f'    assert Path(portable_roi.__file__).resolve().is_relative_to(Path({str(bundle)!r}))\n'
        '    assert Path.cwd() == Path("/tmp")\n'
        f'    assert not Path({str(ROOT/"tgo/cli.py")!r}).exists()\n'
        '    print("TGO_PORTABLE_HELPER", portable_roi.__file__)\n')
    (output/'vllm_decode.py').write_text(code.replace('if __name__ == "__main__":\n',
        'if __name__ == "__main__":\n'+checks))
    adjacent=output/'python3.tgo';shutil.copy2(build['entry'],adjacent)
    wrapper=output/'python-wrapper.sh'
    wrapper.write_text('#!/bin/sh\nset -eu\ncd /tmp\nexec '+ADJACENT+' "$@"\n');wrapper.chmod(0o755)
    sudo=['sudo','-n'] if args.sudo else []
    def contents(path):return subprocess.check_output([*sudo,'cat',str(path)])
    commands=[]
    for arm in ['candidate','disabled']:
        command=[*sudo,'docker','run','--rm','--pid','host','--network','none',
            '--cpuset-cpus',args.cpus or str(args.cpu),'--cpuset-mems',args.mems,
            '--memory','40g','--memory-swap','40g','--pids-limit','2048',
            '--cap-add','SYS_ADMIN','--cap-add','IPC_LOCK','--security-opt','seccomp=unconfined',
            '-v',str(bundle)+':'+str(bundle)+':ro','-v',str(output)+':/verification',
            '-v',str(adjacent)+':'+ADJACENT+':ro','-v',str(models)+':/models:ro']
        env={'PY':'/verification/python-wrapper.sh','TGO_RESULT_FILE':'/verification/'+arm+'.result.json',
            'TGO_IMAGE_LOG':'/verification/'+arm,'VLLM_ENABLE_V1_MULTIPROCESSING':'0',
            'OMP_NUM_THREADS':'1','TASK_QUEUE_ENABLE':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
            'XDG_CACHE_HOME':'/verification/cache','TGO_DISABLE':'1' if arm=='disabled' else '0'}
        for key,value in env.items():command+=['-e',key+'='+value]
        command+=['--entrypoint','/bin/bash',IMAGE,'/opt/va26-scripts/run_real_test_official.sh',
            '/verification/vllm_decode.py','--requests','9','--warmup-requests','1','--roi-cpu',str(args.cpu),
            '--max-tokens','8','--no-detokenize','--check-native-ops']
        commands.append(command)
        with (output/(arm+'.launch.log')).open('wb') as log:
            subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
    results={arm:json.loads(contents(output/(arm+'.result.json'))) for arm in ['candidate','disabled']}
    observed={};raw_files=[]
    for arm in results:
        observed[arm]=set()
        for p in output.glob(arm+'.*.images'):
            data=contents(p)
            observed[arm].update(line.split('\t',2)[-1] for line in data.decode().splitlines())
            raw_files.append({'path':str(p),'sha256':hashlib.sha256(data).hexdigest()})
    evidence={'schema':'tgo.standalone-entry.v1','scope':'functional/load check, not performance confirmation',
        'bundle':str(bundle),'adjacent_entry':ADJACENT,'entry_sha256':hashlib.sha256(adjacent.read_bytes()).hexdigest(),
        'build_sha256':hashlib.sha256((bundle/'build.json').read_bytes()).hexdigest(),
        'image_sha256':{r['optimized']:r['output']['sha256'] for r in build['images']},'map_files':raw_files,
        'commands':commands,'source_tree_mounted':False,'working_directory':'/tmp',
        'correctness_equal':results['candidate']==results['disabled'],
        'loaded_optimized':sorted(observed['candidate']&expected),
        'missing_optimized':sorted(expected-observed['candidate']),
        'disabled_loaded_optimized':sorted(observed['disabled']&expected)}
    passed=evidence['correctness_equal'] and not evidence['missing_optimized'] and not evidence['disabled_loaded_optimized']
    evidence['passed']=passed
    (output/'entry-smoke.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence,indent=2))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
