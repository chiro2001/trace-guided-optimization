import json,os,subprocess
from pathlib import Path
root=Path(os.environ['TGO_PROJECT'])
perf=root/'deps/install/bin/perf'
mode=os.environ['TGO_PROBE_MODE']
out=Path(os.environ.get('TGO_PROBE_OUTPUT', str(root/'build'/('overlay-perf-'+mode))));out.mkdir(parents=True,exist_ok=False)
results=[]
for i in range(3):
 data=out/f'{i}.perf.data'
 command=[str(perf),'record','-o',str(data),'-e','cycles:u','-F','999']
 if mode=='no-cache':command+=['--no-buildid-cache']
 command+=['--','/usr/local/python3.12.13/bin/python3','-c','s=0\nfor i in range(5000000):s+=i\nprint(s)']
 subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 text=subprocess.check_output([str(perf),'script','--show-mmap-events','--no-itrace','-i',str(data)],text=True,stderr=subprocess.PIPE)
 (out/f'{i}.mmaps').write_text(text)
 report=subprocess.check_output([str(perf),'report','-v','--stdio','--no-children','--sort=dso','--field-separator=\t','-i',str(data)],text=True,stderr=subprocess.PIPE)
 (out/f'{i}.hotspots').write_text(report)
 mmaps=[line for line in text.splitlines() if 'PERF_RECORD_MMAP' in line]
 results.append({'round':i,'deleted_mmaps':sum('/ (deleted)' in line for line in mmaps), 'python_mmaps':sum('/usr/local/python3.12.13/lib/libpython3.12.so.1.0' in line for line in mmaps),'hotspots':report})
(out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps([{k:v for k,v in r.items() if k!='hotspots'} for r in results],indent=2))
