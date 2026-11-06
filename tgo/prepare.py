"""准备不可覆盖的优化输入；保留FILE/函数符号，逐范围证明装载内容未变。"""
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import time
from .elf import ELF
from .util import run,tool,sha256,write_json

def allocated(elf):
    rows=[]
    for name,section in elf.sections.items():
        if not section[2]&2:continue
        digest=None
        if section[1]!=8:
            state=hashlib.sha256();offset,remaining=section[4:6]
            while remaining:
                size=min(1048576,remaining);state.update(elf.read(offset,size));offset+=size;remaining-=size
            digest=state.hexdigest()
        rows.append({'name':name,'type':section[1],'flags':section[2],'vaddr':section[3],
            'size':section[5],'alignment':section[8],'sha256':digest})
    return sorted(rows,key=lambda row:(row['vaddr'],row['name'],row['type']))

def symbols(elf):
    rows=[];files=[]
    for symbol in elf.iter_symbols():
        if symbol['type']==4:files.append(symbol['name'])
        if symbol['type']==2 and symbol['shndx']:
            rows.append((symbol['name'],symbol['binding'],symbol['value'],symbol['size']))
    return {'functions':len(rows),'function_provenance_sha256':hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),
        'file_symbols':len(files),'file_provenance_sha256':hashlib.sha256(json.dumps(sorted(files)).encode()).hexdigest()}

def prepare(image,output,strip_debug=False,objcopy=None):
    source=Path(image).resolve();before=ELF(source)
    root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False,mode=0o700);target=root/'image'/source.name
    target.parent.mkdir()
    state={'schema':'tgo.prepared-input.v1','state':'preparing','source':str(source),'image':str(target),
        'strip_debug':strip_debug,'known_input_allowlist':False,'started_unix':time.time(),
        'purpose':'只准备本次输入，不根据已知文件SHA决定优化资格；后续重新采集并原生验证'}
    write_json(root/'input.json',state)
    try:
        if strip_debug:
            cross=tool('aarch64-linux-gnu-objcopy',required=False) if before.machine==183 and platform.machine()!='aarch64' and not objcopy else None
            binary=cross or tool('objcopy',objcopy)
            state['command']=run([binary,'--strip-debug','--keep-file-symbols',str(source),str(target)],root/'objcopy.log',timeout=600)
        else:shutil.copy2(source,target)
        after=ELF(target)
        fields=['type','machine','entry','interpreter','loads']
        if any(getattr(before,key)!=getattr(after,key) for key in fields):raise ValueError('准备输入改变了ELF执行或PT_LOAD布局')
        original,prepared=allocated(before),allocated(after)
        if original!=prepared:raise ValueError('准备输入改变了ALLOC代码、数据或BSS形状')
        original_symbols,prepared_symbols=symbols(before),symbols(after)
        if original_symbols!=prepared_symbols:raise ValueError('准备输入丢失或改变了FILE/函数符号')
        before.assert_unchanged();after.assert_unchanged()
        state.update(state='complete',source_sha256=sha256(source),image_sha256=sha256(target),
            source_bytes=before._size,image_bytes=after._size,all_allocated_sections_unchanged=True,
            allocated_sections=prepared,symbols=prepared_symbols)
        before.assert_unchanged();after.assert_unchanged();return state
    except BaseException as error:state.update(state='failed',error=str(error));raise
    finally:state['finished_unix']=time.time();write_json(root/'input.json',state)
