import hashlib,json,os,subprocess,tempfile
from pathlib import Path
root=Path(os.environ['TGO_PROJECT']); bundle=root/'runs/vllm-single-full-0.4.0-r1'
v=json.loads((bundle/'tune.json').read_text())
with tempfile.TemporaryDirectory(prefix='tgo-vllm-portable-') as tmp:
 env=os.environ.copy();env.update(PY='/usr/local/python3.12.13/bin/python3.tgo',TGO_IMAGE_LOG=tmp+'/maps',TGO_RESULT_FILE=tmp+'/result.json')
 target=root/'benchmarks/vllm_decode.py'
 bridge=Path(tmp)/'workload.py'
 argv=[str(target),'--requests','16','--warmup-requests','1','--roi-cpu','236']
 bridge.write_text('import os,runpy,sys\nos.chdir('+repr(tmp)+')\nsys.argv='+repr(argv)+'\nrunpy.run_path('+repr(str(target))+',run_name="__main__")\n')
 command=['/bin/bash','/opt/va26-scripts/run_real_test_official.sh',str(bridge)]
 with open(tmp+'/stdout','w') as out,open(tmp+'/stderr','w') as err:
  trial=subprocess.run(command,cwd=tmp,env=env,stdout=out,stderr=err,timeout=600)
 if trial.returncode:
  print(Path(tmp+'/stderr').read_text()[-3000:]);raise RuntimeError('portable vLLM entry failed')
 images='\n'.join(p.read_text() for p in Path(tmp).glob('maps.*.images'))
 if not all(path in images for path in v['optimized_images']):raise RuntimeError('missing optimized vLLM DSO')
 digest=hashlib.sha256(json.dumps(json.loads(Path(tmp+'/result.json').read_text()),sort_keys=True,allow_nan=False).encode()).hexdigest()
 expected=v['confirmation']['pairs'][0]['candidate']['correctness_sha256']
 if digest!=expected:raise RuntimeError(f'token result differs: {digest} != {expected}')
 print(json.dumps({'schema':'tgo.smoke.vllm-portable.v1','passed':True,'entry':'/usr/local/python3.12.13/bin/python3.tgo','bundle':str(bundle),'cwd':'temporary directory outside project','project_mount':'read-only','appimage_invoked':False,'loaded_optimized':v['optimized_images'],'correctness_sha256':digest,'ROI_CPU':236},indent=2))
