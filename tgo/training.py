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
        if config['kind']!='gcc':raise ValueError('unsupported training runtime')
        if sha256(config['library'])!=config['sha256']:raise ValueError('training library changed')
        if config['library'] not in Path('/proc/self/maps').read_text():raise ValueError('training library is not mapped')
        library=ctypes.PyDLL(config['library'])
        reset,dump=library.__gcov_reset,library.__gcov_dump
        for f in (reset,dump):f.argtypes=[];f.restype=None
        _hooks=(reset,dump,config)
    _hooks[0]()


def end():
    global _count
    if _hooks is None:return
    _hooks[1]();_count+=1
    with open(_hooks[2]['log'],'a') as stream:
        stream.write(json.dumps({'pid':os.getpid(),'completed_roi':_count,'kind':'gcc'})+'\n')
