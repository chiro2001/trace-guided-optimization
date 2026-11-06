"""为当前映像生成保留原数据/函数身份的在线代码包，不查应用SHA名单。"""
from collections import defaultdict
from pathlib import Path
import os
import subprocess
import time
from tgo.capabilities import inspect_image
from tgo.elf import ELF as CheckedELF,normalize_loads
from tgo.util import read_json,write_json,sha256,tool,run,tool_env,IdentityError
from tgo.optimize import tool_capabilities
from .aarch64_elf import ELF
from .private_tables import describe
from .correspondence import functions as function_correspondence

def profile_input(bundle,image):
    bundle=Path(bundle).resolve();built=read_json(bundle/'build.json');requested=str(Path(image).resolve())
    matches=[row for row in built['images'] if requested in {str(Path(row['original']).resolve()),str(Path(row.get('rewrite_input',row['input'])['path']).resolve())}]
    if not matches:
        digest=sha256(requested)
        matches=[row for row in built['images'] if row.get('rewrite_input',row['input'])['sha256']==digest]
    if len(matches)!=1:raise ValueError('画像工件中没有唯一目标映像:'+requested)
    row=matches[0];record=row.get('rewrite_input',row['input']);source=Path(requested)
    profile=Path(row['optimized']).parent/'profile.fdata'
    if sha256(source)!=record['sha256']:raise IdentityError('在线输入与实际BOLT画像输入不一致')
    if sha256(profile)!=row['fdata_sha256']:raise IdentityError('BOLT边画像发生变化')
    return {'source':str(source),'source_sha256':record['sha256'],'fdata_path':str(profile),
        'fdata_sha256':row['fdata_sha256'],'profile_bundle':str(bundle),'profile_build_sha256':sha256(bundle/'build.json'),
        'qualification':'当前输入与本次画像绑定；没有已知应用哈希名单'}

def inspect(source,output):
    old,new=ELF(source),ELF(output)
    original_hi=max(s['addr']+s['size'] for s in old.sections if s['flags']&2)
    additions=[s for s in new.sections if s['flags']&2 and s['size'] and s['addr']>=original_hi]
    changes=[]
    for section in old.sections:
        if not section['flags']&2 or section['type']==8:continue
        generated=new.by_name.get('.bolt.org'+section['name'],new.by_name.get(section['name']))
        if not generated or generated['addr']!=section['addr'] or generated['size']!=section['size']:
            changes.append({'name':section['name'],'shape_changed':True});continue
        a,b=old.blob(section),new.blob(generated)
        count=sum(x!=y for x,y in zip(a,b))
        if count:changes.append({'name':section['name'],'flags':section['flags'],'changed_bytes':count})
    functions,rejected=function_correspondence(old,new,additions)
    return {'schema':'tgo.live-inspection.v1','new_sections':additions,'original_allocated_changes':changes,'functions':functions,'rejected':rejected,
        'function_count':len(functions),'original_function_count':len(functions)+len(rejected)}

def build(reference,folder,vma,bolt=None,lite=False,function_order=None,capsule=False,layout='ext-tsp',optimization_level='aggressive'):
    source=Path(reference['source']);structure=inspect_image(source)
    if not structure['online']['static_candidate']:raise ValueError('在线静态资格不满足:'+','.join(structure['online']['requirements']))
    if sha256(source)!=reference['source_sha256'] or sha256(reference['fdata_path'])!=reference['fdata_sha256']:raise IdentityError('准备期间输入或画像变化')
    generator=tool('llvm-bolt-live',bolt)
    generator_sha256=sha256(generator)
    help_text=tool_capabilities(generator,{})
    for flag in ['runtime-preserve-function-identity','custom-allocation-vma']:
        if flag not in help_text:raise ValueError('生成器缺少在线能力:'+flag)
    if capsule and 'runtime-online-capsule' not in help_text:raise ValueError('生成器缺少runtime-online-capsule能力')
    from tgo.tune import LAYOUTS
    if layout not in LAYOUTS or optimization_level not in ('conservative','balanced','aggressive'):raise ValueError('未知在线布局或优化级别')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False);output=folder/source.name
    runtime=structure['runtime'];order=function_order or ('cdsort' if runtime=='cpython' else 'hfsort+')
    command=[generator,str(source),'-data='+reference['fdata_path'],'-o',str(output),'-custom-allocation-vma='+str(vma),
        '-runtime-preserve-function-identity','-reorder-blocks=ext-tsp','-reorder-functions='+order,
        '-split-functions','-split-strategy=profile2','-split-all-cold','-cold-threshold=0','-split-eh',
        '-align-functions=4','-align-functions-max-bytes=4','-thread-count=4','-allow-stripped',
        '-enable-bat','-group-stubs=false','-trap-old-code=false','-use-old-text=false']
    if optimization_level!='conservative':command+=['-eliminate-unreachable','-peepholes=all']
    if optimization_level=='aggressive':command+=['-simplify-conditional-tail-calls','-sctc-mode=always']
    command+=LAYOUTS[layout]
    if runtime=='cpython':command+=['-jump-tables=move','-skip-funcs=call_weak_fn|.*sre_ucs[124]_match.*']
    else:command+=['-skip-funcs=call_weak_fn']
    if not structure['retained_code_relocations']:
        for flag in ['pgo128-recover-code-relocs','pgo128-trampoline-mode']:
            if flag not in help_text:raise ValueError('无保留重定位的在线输入需要生成器能力:'+flag)
        command+=['-relocs','-force-patch','-pgo128-trampoline-mode','-pgo128-recover-code-relocs','-aarch64-relaxation-non-reloc']
    if lite:command+=['-lite','-lite-threshold-count=1']
    if capsule:command+=['-runtime-online-capsule']
    started=time.monotonic_ns();result=run(command,folder/'build.log',timeout=600)
    if sha256(generator)!=generator_sha256:raise IdentityError('生成期间BOLT工具发生变化')
    if sha256(source)!=reference['source_sha256'] or sha256(reference['fdata_path'])!=reference['fdata_sha256']:
        raise IdentityError('生成期间输入或画像发生变化')
    raw=output.with_name(output.name+'.raw');output.rename(raw);normalization=normalize_loads(raw,output)
    CheckedELF(output)
    report=inspect(source,output)
    report['build']={**reference,'tool_sha256':generator_sha256,'tool':generator,'command':command,
        'runtime_identity':True,'plt_optimized':False,'runtime':runtime,'online_capsule':capsule,
        'layout':layout,'optimization_level':optimization_level,'lite':lite,
        'standalone_executable':False,'result':result,'normalization':normalization,
        'started_monotonic_ns':started,'finished_monotonic_ns':time.monotonic_ns()}
    if runtime=='cpython':report.update(describe(ELF(source),ELF(output),report,vma))
    report['output_sha256']=sha256(output)
    write_json(folder/'inspection.json',report)
    write_json(folder/'summary.json',{'schema':'tgo.live-inspection-summary.v1','source':str(source),
        'source_sha256':reference['source_sha256'],'original_function_count':report['original_function_count'],
        'generated_function_count':report['function_count'],'unmapped_function_count':len(report['rejected']),
        'unmapped_examples':report['rejected'][:100],'unmapped_examples_truncated':len(report['rejected'])>100,
        'private_tables':[{'name':t['name'],'owner':t['owner'],'entries':t['entries']} for t in report.get('private_readonly_tables',[])],
        'online_capsule':capsule,'standalone_executable':False,'known_input_allowlist':False})
    return report
