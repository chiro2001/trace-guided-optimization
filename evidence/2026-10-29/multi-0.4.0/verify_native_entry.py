import json,os,subprocess,sys,tempfile
from pathlib import Path
root=Path.cwd(); bundle=root/'runs'/sys.argv[1]
v=json.loads((bundle/'tune.json').read_text())
env=os.environ.copy();env['PATH']=''
for key in ('PYTHONPATH','PYTHONHOME','TGO_RUNTIME_DIR'):env.pop(key,None)
with tempfile.TemporaryDirectory(prefix='tgo-entry-independent-') as outside:
 env['TGO_IMAGE_LOG']=outside+'/mapped'
 command=[v['entry'],str(root/'build/standalone_native.py'),'--library',str(root/'build/bench/libfrontend.so'),'--iterations','2000000']
 run=subprocess.run(command,cwd=outside,env=env,capture_output=True,text=True,check=True)
 maps='\n'.join(p.read_text() for p in Path(outside).glob('mapped.*.images'))
 if not v['optimized_images'] or not all(path in maps for path in v['optimized_images']):raise RuntimeError('missing optimized DSO mappings')
 report={'schema':'tgo.smoke.independent-entry.v1','passed':True,'entry':v['entry'],'cwd':'temporary directory outside project','PATH':'','PYTHONPATH':None,'stdout':run.stdout,'loaded_optimized':v['optimized_images'],'appimage_invoked':False}
 (bundle/'independent-entry.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps(report,indent=2))
