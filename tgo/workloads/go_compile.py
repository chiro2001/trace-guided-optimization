"""确定性Go编译负载，显式区分冷构建缓存与缓存保留。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
from tgo.roi import region,emit_result
from tgo.util import sha256,write_json

def prepare(output,packages=16,functions=96,seed=101):
    if min(packages,functions)<=0:raise ValueError('packages和functions必须为正数')
    root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False)
    (root/'go.mod').write_text('module example.test/frontend\n\ngo 1.20\n')
    rng=random.Random(seed);sources=['go.mod']
    for package in range(packages):
        folder=root/f'p{package:03d}';folder.mkdir()
        lines=[f'package p{package:03d}\n']
        for function in range(functions):
            constant=rng.randrange(1,2**32)
            lines.append(f'''func F{function}(x uint64) uint64 {{
 y := x ^ {constant}
 for k := uint64(0); k < 5; k++ {{
  switch (y+k)&7 {{
  case 0: y += (x<<3) ^ k
  case 1, 3: y ^= (y>>7) + {constant}
  case 2: y = y*33 + x
  case 4, 6: y += (y<<5) ^ (x>>2)
  default: y = (y>>3) ^ (y*17)
  }}
 }}
 return y
}}
''')
        lines.append('func Sum(x uint64) uint64 {\n')
        lines.extend(f'x=F{function}(x)\n' for function in range(functions))
        lines.append('return x\n}\n')
        relative=f'p{package:03d}/work.go';(root/relative).write_text(''.join(lines));sources.append(relative)
    lines=['package main\nimport (\n']
    lines.extend(f'"example.test/frontend/p{package:03d}"\n' for package in range(packages))
    lines+=[')\nfunc main() {\nx:=uint64(7)\n']
    lines.extend(f'x=p{package:03d}.Sum(x)\n' for package in range(packages))
    lines.append('println("go-workload-ok",x)\n}\n')
    (root/'main.go').write_text(''.join(lines));sources.append('main.go')
    manifest={'schema':'tgo.go-fixture.v1','packages':packages,'functions_per_package':functions,'seed':seed,
        'sources':{name:sha256(root/name) for name in sources}}
    write_json(root/'fixture.json',manifest)
    return manifest

def run_fixture(fixture,go=None,cache='cold',workdir=None):
    root=Path(fixture).resolve();manifest=json.loads((root/'fixture.json').read_text())
    for name,expected in manifest['sources'].items():
        path=(root/name).resolve();path.relative_to(root)
        if sha256(path)!=expected:raise ValueError('Go固定工作集在测量前发生变化:'+name)
    go=go or shutil.which('go')
    if not go:raise ValueError('找不到Go工具链，请传--go')
    temporary=None
    if workdir:out=Path(workdir).resolve();out.mkdir(parents=True,exist_ok=False)
    elif os.environ.get('TGO_RESULT_FILE'):
        out=Path(os.environ['TGO_RESULT_FILE']).with_suffix('.go-work');out.mkdir(exist_ok=False)
    else:
        temporary=tempfile.TemporaryDirectory(prefix='tgo-go-compile-');out=Path(temporary.name)
    env=os.environ.copy()
    env.update(GOTOOLCHAIN='local',GOWORK='off',GOPROXY='off',GOSUMDB='off',CGO_ENABLED='0')
    own_cache=False
    if env.get('TGO_VALIDATION_GOCACHE'):env['GOCACHE']=env['TGO_VALIDATION_GOCACHE']
    elif cache=='cold':env['GOCACHE']=str(out/'cache');own_cache=True
    else:env['GOCACHE']=str(root/'.benchmark-build-cache')
    target=out/'program'
    argv=[str(go),'build','-trimpath','-o',str(target),'.']
    try:
        with (out/'build.stdout').open('wb') as stdout,(out/'build.stderr').open('wb') as stderr:
            with region('go-compile'):
                result=subprocess.run(argv,cwd=root,env=env,stdout=stdout,stderr=stderr)
        if result.returncode:raise RuntimeError('Go编译失败，见'+str(out/'build.stderr'))
        result=subprocess.run([str(target)],cwd=root,env=env,capture_output=True,text=True,check=True)
        text=result.stderr.strip()
        fields=text.split()
        if len(fields)!=2 or fields[0]!='go-workload-ok' or not fields[1].isdigit():raise RuntimeError('Go生成程序的语义输出不符')
        evidence={'build_command':argv,'gocache':env['GOCACHE'],'cache_mode':cache,'program_sha256':sha256(target),
            'program_stdout':result.stdout,'program_stderr':result.stderr}
        write_json(out/'evidence.json',evidence)
        output={'workload':'go-compile','seed':manifest['seed'],'packages':manifest['packages'],
            'functions_per_package':manifest['functions_per_package'],'cache':cache,'semantic_result':int(fields[1])}
        emit_result(output);print(json.dumps(output,sort_keys=True))
        return output
    finally:
        if own_cache:shutil.rmtree(env['GOCACHE'],ignore_errors=True)
        if temporary:temporary.cleanup()

def main(argv=None):
    p=argparse.ArgumentParser(description='Go编译前端工作集')
    commands=p.add_subparsers(dest='action',required=True)
    create=commands.add_parser('prepare');create.add_argument('-o','--output',required=True)
    create.add_argument('--packages',type=int,default=16);create.add_argument('--functions',type=int,default=96);create.add_argument('--seed',type=int,default=101)
    run=commands.add_parser('run');run.add_argument('fixture');run.add_argument('--go');run.add_argument('--cache',choices=['cold','preserve'],default='cold');run.add_argument('--workdir')
    args=p.parse_args(argv)
    if args.action=='prepare':
        result=prepare(args.output,args.packages,args.functions,args.seed);print(json.dumps(result,ensure_ascii=False,indent=2));return result
    else:return run_fixture(args.fixture,args.go,args.cache,args.workdir)
if __name__=='__main__':main()
