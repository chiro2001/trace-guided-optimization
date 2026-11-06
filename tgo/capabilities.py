"""目标资格只依赖可观察结构；SHA仅由采集/事务层用于绑定同一次输入。"""
from pathlib import Path
from copy import deepcopy
from functools import lru_cache
import os
import struct
from .elf import ELF, is_executable_image

SCHEMA = 'tgo.capabilities.v1'

def gnu_features(elf):
    result = {'bti':False,'pac':False,'ibt':False,'shstk':False}
    for section in elf.section_entries:
        if section[1] != 7:
            continue
        raw=elf.read(section[4],section[5]);pos=0
        while pos+12 <= len(raw):
            namesz,descsz,kind=struct.unpack_from('<III',raw,pos);pos+=12
            name=raw[pos:pos+namesz];pos+=(namesz+3)&~3
            end=pos+descsz
            if end>len(raw):raise ValueError('truncated ELF note')
            if name==b'GNU\0' and kind==5:
                while pos<end:
                    if pos+8>end:raise ValueError('truncated GNU property')
                    typ,size=struct.unpack_from('<II',raw,pos);pos+=8
                    if pos+size>end:raise ValueError('GNU property outside note')
                    if typ in (0xc0000000,0xc0000002):
                        if size!=4:raise ValueError('unexpected GNU feature width')
                        value=struct.unpack_from('<I',raw,pos)[0]
                        if elf.machine==183 and typ==0xc0000000:result.update(bti=bool(value&1),pac=bool(value&2))
                        if elf.machine==62 and typ==0xc0000002:result.update(ibt=bool(value&1),shstk=bool(value&2))
                    pos+=(size+7)&~7
            pos=(end+3)&~3
    return result

def inspect_image(path):
    path=Path(path).resolve();stat=path.stat()
    return deepcopy(_cached_image(str(path),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,os.sysconf('SC_PAGE_SIZE')))

@lru_cache(maxsize=128)
def _cached_image(path,*stamp):
    return _inspect_image(path)

def _inspect_image(path):
    """Read-only; intentionally never computes or compares an application digest."""
    elf=ELF(path);defined=set();references=set();functions=0;mapping={};files=0;cold=0
    for symbol in elf.iter_symbols():
        name=symbol['name']
        if symbol['type']==4:files+=1
        if symbol['shndx']:
            if name.startswith(('$x','$d')):mapping.setdefault(symbol['shndx'],set()).add((symbol['value'],name[1]))
            if symbol['type']==2 and symbol['size']:
                functions+=1
                if '.cold' in name:cold+=1
        if name.startswith(('Py','_Py','__gxx','runtime.','main.')):
            references.add(name)
            if symbol['shndx']:defined.add(name)
    go=any('gopclntab' in name or name=='.go.buildinfo' for name in elf.sections) or bool({'runtime.firstmoduledata','runtime.pclntab'}&defined)
    python_core={'Py_GetVersion','PyObject_Call'}<=defined and bool({'_PyEval_EvalFrameDefault','PyEval_EvalFrameEx'}&defined)
    python_entry=bool({'Py_BytesMain','Py_Main'}&references) and not python_core
    runtime='go' if go else 'cpython' if python_core else 'native'
    features=gnu_features(elf)
    role={1:'relocatable-object',4:'core-dump'}.get(elf.type) or ('executable' if is_executable_image({'type':elf.type,'interpreter':elf.interpreter,'pie':bool(elf.dynamic_values.get(0x6ffffffb,0)&0x08000000)}) else 'dso')
    offline=[];online=[]
    if elf.machine not in (62,183):offline.append('unsupported_isa')
    if elf.type not in (2,3):offline.append('offline_backend_requires_executable_or_dso')
    if not elf.section_entries:offline.append('section_headers_required')
    if go:offline.append('go_pc_stack_gc_metadata_requires_go_adapter')
    if not functions:offline.append('function_discovery_required')
    if elf.machine!=183:online.append('online_backend_requires_aarch64')
    if os.sysconf('SC_PAGE_SIZE')!=4096:online.append('online_backend_requires_4k_os_pages')
    if elf.type!=3:online.append('online_backend_requires_et_dyn')
    if go:online.append('go_pc_stack_gc_metadata_requires_go_adapter')
    if '.eh_frame' not in elf.sections:online.append('unwind_registration_required')
    if not functions:online.append('function_discovery_required')
    if features['bti'] or features['pac']:online.append('bti_pac_adapter_required')
    if python_core:
        tables=[s['name'] for s in elf.iter_symbols() if s['shndx'] and s['type']==1 and s['name'].startswith(('opcode_targets.','sre_targets.'))]
        if not any(name.startswith('opcode_targets.') for name in tables):online.append('cpython_private_dispatch_table_discovery_required')
    else:tables=[]
    elf.assert_unchanged()
    notes=['候选资格不包含固定文件名或已知SHA白名单','当前版本输入与画像/事务一致性仍由运行时采集身份核验']
    if cold and not files and elf.code_relocation_sections():
        notes.append('存在冷片段名称但缺少FILE符号；部分BOLT版本无法聚合，应从含FILE的原件用prepare --strip-debug保留出处')
    return {'schema':SCHEMA,'path':str(elf.path),'eligibility_basis':'elf-structure-and-tool-capabilities',
        'known_input_allowlist':False,'architecture':{62:'x86_64',183:'aarch64'}.get(elf.machine,str(elf.machine)),
        'elf_type':elf.type,'role':role,'runtime':runtime,'cpython_entry':python_entry,
        'function_symbols':functions,'file_symbols':files,'cold_fragment_symbols':cold,
        'retained_code_relocations':bool(elf.code_relocation_sections()),
        'security_features':features,'private_dispatch_tables':tables,'host_page_size':os.sysconf('SC_PAGE_SIZE'),
        'offline':{'candidate':not offline,'requirements':offline},
        'online':{'static_candidate':not online,'requirements':online,
            'runtime_checks':['same_input_as_profile','identity_preserving_generator','original_state_and_tls','new_code_cfi_and_readonly_tables','near_entry_branch_plan','all_thread_transaction_and_rollback']},
        'recovery':{'assume_code_pages_safe':False,'reason':'未独立证明全部恢复地址与嵌入数据边界，通用策略不启用assume-code-pages-safe'},
        'notes':notes}

def classify_profile_image(row):
    """Historical capture metadata is descriptive; live structure wins when readable."""
    path=Path(row['path'])
    if path.is_file():return inspect_image(path)
    return {'path':str(path),'runtime':'go' if row.get('go') else 'unknown',
            'offline':{'candidate':False,'requirements':['input_unavailable']},
            'online':{'static_candidate':False,'requirements':['input_unavailable']},
            'recovery':{'assume_code_pages_safe':False,'reason':'input_unavailable'}}
