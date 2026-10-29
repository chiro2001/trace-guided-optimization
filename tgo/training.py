"""ROI-only compiler instrumentation hooks; inactive during native validation."""
import ctypes
import json
import os
from pathlib import Path
from .util import sha256

_hooks = None
_count = 0


def begin():
    global _hooks
    spec = os.environ.get('TGO_TRAINING_HOOKS')
    if not spec:return
    if _hooks is None:
        config=json.loads(Path(spec).read_text())
        if sha256(config['library'])!=config['sha256']:raise ValueError('training library changed')
        mappings=[]
        for line in Path('/proc/self/maps').read_text().splitlines():
            fields=line.split(None,5)
            if len(fields)==6 and fields[5]==config['library']:
                lo,hi=(int(v,16) for v in fields[0].split('-'))
                mappings.append((lo,hi,fields[1],int(fields[2],16)))
        if not mappings:raise ValueError('training library is not mapped')
        if config['kind']=='gcc':
            library=ctypes.PyDLL(config['library'])
            reset,dump=library.__gcov_reset,library.__gcov_dump
            for f in (reset,dump):f.argtypes=[];f.restype=None
        elif config['kind']=='bolt':
            bases={lo-config['zero_offset_load_vaddr'] for lo,hi,flags,offset in mappings if offset==0}
            if len(bases)!=1:raise ValueError('ambiguous BOLT runtime load bias')
            bias=bases.pop()
            for key in ('reset_va','dump_va'):
                if not any(lo<=bias+config[key]<hi and 'x' in flags for lo,hi,flags,offset in mappings):
                    raise ValueError('BOLT runtime hook outside executable mappings')
            reset=ctypes.PYFUNCTYPE(None)(bias+config['reset_va'])
            dump=ctypes.PYFUNCTYPE(None,ctypes.c_int,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_uint64)(bias+config['dump_va'])
        else:raise ValueError('unsupported training runtime')
        _hooks=(reset,dump,config)
    _hooks[0]()


def end():
    global _count
    if _hooks is None:return
    _count+=1
    profile=None
    if _hooks[2]['kind']=='gcc':
        _hooks[1]()
    else:
        profile=Path(_hooks[2]['directory'])/f'profile-{os.getpid()}-{_count:04d}.fdata'
        fd=os.open(profile,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644)
        try:_hooks[1](fd,os.fsencode(_hooks[2]['library']),None,0)
        finally:os.close(fd)
        if profile.stat().st_size<100:raise ValueError('native BOLT emitted an empty ROI profile')
    with open(_hooks[2]['log'],'a') as stream:
        stream.write(json.dumps({'pid':os.getpid(),'completed_roi':_count,'kind':_hooks[2]['kind'],
            'profile':str(profile) if profile else None,'sha256':sha256(profile) if profile else None})+'\n')
