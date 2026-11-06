#!/usr/bin/env python3
"""新代码与原数据共享load bias；独立近端入口页，旧主体完整保留。"""
from __future__ import annotations
import collections,ctypes,struct,sys,time,subprocess
from .common import *
from .aarch64_elf import ELF,parse_maps,branch_word,sx
from .eh_frame import relocate
from tgo.util import tool,tool_env

def gaps(low,high):
    cursor=low;out=[]
    for row in parse_maps(Path('/proc/self/maps').read_text()):
        if row['end']<=low:continue
        if row['start']>=high:break
        if cursor<row['start']:out.append((cursor,min(row['start'],high)))
        cursor=max(cursor,row['end'])
    if cursor<high:out.append((cursor,high))
    return out

def veneer(pc,target):
    delta=((target&~4095)-(pc&~4095))//4096
    if not -(1<<20)<=delta<(1<<20):raise ValueError('ADRP范围')
    imm=delta&0x1fffff
    words=[0x90000010|((imm&3)<<29)|((imm>>2)<<5),0x91000210|((target&4095)<<10),0xd61f0200]
    decoded=(pc&~4095)+sx(((words[0]>>29)&3)|(((words[0]>>5)&0x7ffff)<<2),21)*4096+((words[1]>>10)&4095)
    if decoded!=target:raise ValueError('跳板回算失败')
    return struct.pack('<III',*words)

def leaf_frame(address,size):
    instructions=bytes([0x0c,31,0,0x08,30])+b''.join(bytes([0x08,r]) for r in range(19,30))
    cie=struct.pack('<I',0)+bytes([1])+b'zR\0'+bytes([1,0x78,30,1,0])+instructions
    cie+=bytes(-len(cie)%4);record=struct.pack('<I',len(cie))+cie
    fde=struct.pack('<IQQB',len(record)+4,address,size,0);fde+=bytes(-len(fde)%4)
    return record+struct.pack('<I',len(fde))+fde+bytes(4)

def supplement_weak(w,old,new,f,bias):
    def code(elf,address,size):
        section=next(s for s in elf.sections if s['flags']&4 and s['addr']<=address and address+size<=s['addr']+s['size'])
        offset=section['offset']+address-section['addr'];return elf.data[offset:offset+size]
    def adrp(word,pc):return (pc&~4095)+sx(((word>>29)&3)|(((word>>5)&0x7ffff)<<2),21)*4096
    source=list(struct.unpack('<5I',code(old,f['old_vaddr'],20))) if f['old_size']==20 else []
    if not (len(source)==5 and source[0]&0x9f00001f==0x90000000 and source[1]&0xffc003ff==0xf9400000 and source[2]==0xb4000040 and source[3]&0xfc000000==0x14000000 and source[4]==0xd65f03c0):raise RuntimeError('原call_weak_fn模板不符')
    got_page=adrp(source[0],f['old_vaddr']);tail=f['old_vaddr']+12+sx(source[3]&0x3ffffff,26)*4
    plt,_=old.ordinary_plt();tail_got=plt[tail]['got_offset'] if tail in plt else None
    parts={(f['new_vaddr'],f['new_size'])}
    for symbol in new.symbols:
        if symbol['name'].startswith('call_weak_fn.cold') and symbol['shndx'] and symbol['size']:parts.add((symbol['value'],symbol['size']))
    words={}
    for address,size in parts:
        if size%4:raise RuntimeError('辅助函数指令未对齐')
        words.update({address+i*4:value for i,(value,) in enumerate(struct.iter_unpack('<I',code(new,address,size)))})
    for pc,word in words.items():
        if word in (0xd65f03c0,0xd503201f) or word==source[1]:continue
        if word&0xffe0001f==0xd4200000:continue
        if word&0x9f00001f==0x90000000 and adrp(word,pc)==got_page:continue
        if word&0xff00001f in (0xb4000000,0xb5000000) and pc+sx((word>>5)&0x7ffff,19)*4 in words:continue
        if word&0xfc000000==0x14000000 and pc+sx(word&0x3ffffff,26)*4 in {*words,tail}:continue
        if word&0x9f00001f==0x90000010 and adrp(word,pc) in {tail&~4095,tail_got&~4095 if tail_got else -1}:continue
        if word&0xffc003ff==0xf9400211 and tail_got is not None:
            a=words.get(pc-4,0)
            if a&0x9f00001f==0x90000010 and adrp(a,pc-4)+((word>>10)&4095)*8==tail_got:continue
        if word&0xffc003ff==0x91000210 and ((word>>10)&4095)==tail&4095:continue
        if word==0xd61f0200:
            a,d=words.get(pc-8,0),words.get(pc-4,0)
            if a&0x9f00001f==0x90000010 and d&0xffc003ff==0x91000210 and adrp(a,pc-8)+((d>>10)&4095)==tail:continue
        if word==0xd61f0220 and tail_got is not None:
            a,l=words.get(pc-8,0),words.get(pc-4,0)
            if a&0x9f00001f==0x90000010 and l&0xffc003ff==0xf9400211 and adrp(a,pc-8)+((l>>10)&4095)*8==tail_got:continue
        raise RuntimeError('辅助叶函数含未证明指令:'+hex(pc)+':'+hex(word))
    pending=[f['new_vaddr']];seen=set()
    while pending:
        pc=pending.pop()
        if pc==tail:continue
        if pc in seen:continue
        if pc not in words:raise RuntimeError('辅助函数控制流离开已证明片段')
        seen.add(pc);word=words[pc]
        if word&0xffe0001f==0xd4200000:raise RuntimeError('辅助函数BRK填充可达')
        if word in (0xd65f03c0,0xd61f0200,0xd61f0220):continue
        if word&0xfc000000==0x14000000:pending.append(pc+sx(word&0x3ffffff,26)*4)
        elif word&0xff00001f in (0xb4000000,0xb5000000):pending.extend([pc+4,pc+sx((word>>5)&0x7ffff,19)*4])
        else:pending.append(pc+4)
    result=[]
    for address,size in sorted(parts):
        result.append({'name':'call_weak_fn','address':bias+address,'size':size,'frame_hex':leaf_frame(bias+address,size).hex(),'proof':'原五指令模板；新热/冷块及远跳板逐指令核验，无SP/LR修改，GOT与尾调用目标相同'})
    return result

def reserve(w,cmd):
    name=cmd['name'];bias=w.libraries[name]['load_bias'];size=cmd.get('size',32<<20)
    if not hasattr(w,'reservations'):w.reservations={}
    if name in w.reservations:return w.reservations[name]
    original=ELF(w.manifest['libraries'][name]['path'])
    original_hi=max(p['vaddr']+p['memsz'] for p in original.loads)
    low=max(65536,bias+((original_hi+4095)&~4095));high=min((1<<64)-65536,bias+(1<<31)-65536)
    candidates=[]
    for a,b in gaps(low,high):
        vma=(a-bias+0x1fffff)&~0x1fffff
        address=bias+vma
        if address+size<=b:candidates.append((address,vma))
    attempts=[]
    for address,vma in candidates:
        got=w.helper.rb_reserve(address,size)
        attempts.append({'address':address,'errno':ctypes.get_errno() if not got else 0})
        if got:
            if got!=address:raise RuntimeError('保留地址错误')
            result={'name':name,'address':address,'size':size,'custom_vma':vma,'load_bias':bias,
                'after_llm_ready':True,'attempts':attempts,'monotonic_ns':time.monotonic_ns()}
            w.reservations[name]=result;return result
    raise RuntimeError('找不到库地址范围内的新区域:'+name)

def qualify(source,output,report):
    old,new=ELF(source),ELF(output)
    if any(old.properties()) or any(new.properties()):raise RuntimeError('尚未资格化BTI/PAC')
    # 只读业务表必须保持原样；原动态符号及重定位表变化由旧入口间接覆盖。
    allowed={'.note.gnu.build-id','.dynsym','.rela.dyn','.rela.plt','.init','.text','.fini','.dynamic','.got','.got.plt','.eh_frame','.eh_frame_hdr'}
    entrymap={f['old_vaddr']:f['new_vaddr'] for f in report['functions']}
    # GCC的frame_dummy等入口size为0，仍可按唯一符号身份核对数据中的入口指针。
    newnames=collections.defaultdict(set);oldnames=collections.defaultdict(set)
    for symbol in old.symbols:
        if symbol['type']==2 and symbol['shndx'] and symbol['value']:oldnames[symbol['name']].add(symbol['value'])
    for symbol in new.symbols:
        if symbol['type']==2 and symbol['shndx'] and symbol['value']:
            newnames[symbol['name']].add(symbol['value'])
    for symbol in old.symbols:
        targets=newnames.get(symbol['name'],set())
        origins=oldnames[symbol['name']]
        if symbol['type']==2 and not symbol['size'] and symbol['shndx'] and symbol['value'] and len(targets)==1 and origins=={symbol['value']}:
            value=next(iter(targets))
            if symbol['value'] in entrymap and entrymap[symbol['value']]!=value:continue
            entrymap[symbol['value']]=value
    retained_pointers=[]
    original_relocations={};external_relocations={}
    plt_entries,_=old.ordinary_plt()
    for section in old.sections:
        if section['type']!=4 or not section['flags']&2:continue
        table=old.tables[section['link']]
        for offset,info,addend in struct.iter_unpack('<QQq',old.blob(section)):
            kind,symbol_index=info&0xffffffff,info>>32
            if kind==1027:original_relocations[offset]=addend
            elif kind==257 and symbol_index<len(table) and table[symbol_index]['shndx']:
                original_relocations[offset]=table[symbol_index]['value']+addend
            elif kind==257 and symbol_index<len(table) and not table[symbol_index]['shndx'] and addend==0:
                external_relocations[offset]=table[symbol_index]['name']
    for change in report['original_allocated_changes']:
        if change.get('shape_changed'):raise RuntimeError('原数据形状发生变化:'+str(change))
        if change['name'] not in allowed:
            asec=old.by_name[change['name']];bsec=new.by_name[change['name']]
            if not asec['flags']&1:raise RuntimeError('原只读业务表发生修改:'+str(change))
            a,b=old.blob(asec),new.blob(bsec)
            for offset in range(0,len(a),8):
                x,y=a[offset:offset+8],b[offset:offset+8]
                if x==y:continue
                if len(x)!=8 or asec['addr']%8:raise RuntimeError('原数据变化不是对齐指针')
                oldvalue,newvalue=int.from_bytes(x,'little'),int.from_bytes(y,'little')
                oldvalue=original_relocations.get(asec['addr']+offset,oldvalue)
                private=next((t for t in report.get('private_readonly_tables',[]) if t['old_vaddr']<=asec['addr']+offset<t['old_vaddr']+t['size']),None)
                if private:
                    index=(asec['addr']+offset-private['old_vaddr'])//8
                    if oldvalue!=private['old_values'][index] or newvalue!=private['new_values'][index]:raise RuntimeError('私有只读表翻译核验不符')
                    continue
                external_name=external_relocations.get(asec['addr']+offset)
                same_external=bool(external_name and newvalue in plt_entries and plt_entries[newvalue]['symbol']['name']==external_name)
                same_relocated_value=asec['addr']+offset in original_relocations and oldvalue==newvalue
                if entrymap.get(oldvalue)!=newvalue and not same_external and not same_relocated_value:raise RuntimeError('非函数入口指针变化:'+str((change['name'],hex(asec['addr']+offset),hex(oldvalue),hex(newvalue))))
                retained_pointers.append({'vaddr':asec['addr']+offset,'old_entry':oldvalue,'new_entry':newvalue})
    parts=report['new_sections']
    if any(s['flags']&0x401 or s['type']!=1 for s in parts):raise RuntimeError('不复制新可写数据/TLS/NOBITS')
    # 任何对新区域的动态重定位都必须显式处理，当前先拒绝。
    newrelocs=[]
    for section in new.sections:
        if section['type']!=4 or not section['flags']&2:continue
        for offset,info,addend in struct.iter_unpack('<QQq',new.blob(section)):
            if any(s['addr']<=offset<s['addr']+s['size'] for s in parts):newrelocs.append((offset,info,addend))
    if newrelocs:raise RuntimeError('新区域有未处理动态重定位:'+str(newrelocs[:8]))
    frames=new.by_name.get('.eh_frame')
    if not frames or frames not in parts:raise RuntimeError('缺少新CFI')
    decoded_frames=subprocess.check_output([tool('readelf'),'--debug-dump=frames',str(output)],text=True,env=tool_env())
    if 'DW_CFA_set_loc:' in decoded_frames:raise RuntimeError('尚未实现CFI指令内set_loc地址重定位')
    return old,new,{'original_business_data_unchanged':True,'new_dynamic_relocations':0,
        'same_load_bias':True,'old_text_retained':True,'new_writable_state_copied':False,
        'retained_original_function_pointers':len(retained_pointers),
        'qualification_scope':'已核验能力的BOLT生成器与静态装载约束；需运行时业务与展开门禁'}

def load(w,cmd):
    name=cmd['name'];folder=Path(cmd['folder']).resolve();folder.relative_to(w.workspace/'artifacts')
    report=json.loads((folder/'inspection.json').read_text())
    source=Path(w.manifest['libraries'][name]['path']);output=folder/name
    if sha(source)!=report['build']['source_sha256'] or sha(output)!=cmd['output_sha256']:raise RuntimeError('ELF身份不符')
    old,new,proof=qualify(source,output,report)
    bindings=[]
    if report['build'].get('plt_optimized'):
        handle=ctypes.CDLL(w.libraries[name]['target'],mode=os.RTLD_NOLOAD|os.RTLD_NOW)
        if not hasattr(w,'bound_libraries'):w.bound_libraries=[]
        w.bound_libraries.append(handle)
        plt_entries,_=old.ordinary_plt();plt=old.by_name['.plt'];bias=w.libraries[name]['load_bias']
        for pc,entry in plt_entries.items():
            slot=bias+entry['got_offset'];target=ctypes.c_uint64.from_address(slot).value
            if bias+plt['addr']<=target<bias+plt['addr']+plt['size']:raise RuntimeError('原PLT绑定仍未解析:'+entry['symbol']['name'])
            bindings.append({'slot':slot,'target':target,'symbol':entry['symbol']['name']})
    reservation=w.reservations[name];bias=reservation['load_bias'];pages={}
    for section in report['new_sections']:
        if section['name'] in report.get('omit_payload_sections',[]):continue
        address=bias+section['addr'];end=address+section['size']
        if not reservation['address']<=address<end<=reservation['address']+reservation['size']:raise RuntimeError('新section超出保留区')
        raw=new.blob(section)
        pos=0
        while pos<len(raw):
            page=(address+pos)&~4095
            if page not in pages:pages[page]={'raw':bytearray(4096),'executable':False}
            offset=(address+pos)&4095;length=min(4096-offset,len(raw)-pos)
            pages[page]['raw'][offset:offset+length]=raw[pos:pos+length]
            pages[page]['executable']|=bool(section['flags']&4)
            pos+=length
    for patch in report.get('code_patches',[]):
        address=bias+patch['vaddr'];before=bytes.fromhex(patch['old_hex']);after=bytes.fromhex(patch['new_hex'])
        observed=bytes(pages[(address+i)&~4095]['raw'][(address+i)&4095] for i in range(len(before)))
        if observed!=before or len(before)!=len(after):raise RuntimeError('只读表引用指令原值不符')
        for i,value in enumerate(after):pages[(address+i)&~4095]['raw'][(address+i)&4095]=value
    for table in report.get('private_readonly_tables',[]):
        original=bias+table['old_vaddr'];address=bias+table['new_vaddr']
        rows=[r for r in parse_maps(Path('/proc/self/maps').read_text()) if r['start']<=original and original+table['size']<=r['end']]
        if len(rows)!=1 or 'w' in rows[0]['perms']:raise RuntimeError('原opcode表尚未只读')
        expected_original=b''.join(struct.pack('<Q',bias+value) for value in table['old_values'])
        if ctypes.string_at(original,table['size'])!=expected_original:raise RuntimeError('原分派表的实时值与动态重定位结果不符')
        if not reservation['address']<=address<address+table['size']<=reservation['address']+reservation['size']:raise RuntimeError('私有只读表超出保留区')
        raw=b''.join(struct.pack('<Q',bias+v) for v in table['new_values'])
        offset=0
        while offset<len(raw):
            page=(address+offset)&~4095;within=(address+offset)&4095;length=min(4096-within,len(raw)-offset)
            if page not in pages:pages[page]={'raw':bytearray(4096),'executable':False}
            elif pages[page]['executable']:raise RuntimeError('私有只读表与代码重叠')
            pages[page]['raw'][within:within+length]=raw[offset:offset+length];offset+=length
    functions=[f for f in report['functions'] if f['old_size']>=4 and f['old_vaddr']%4==0]
    # 优先直接B；超距入口分组寻找近端页，不要求全库共用同一地址窗口。
    # 空页不足时保留相应旧入口，其余已资格化函数仍可上线。
    islands=[];entry_targets={};rejected=[];far=[]
    for f in functions:
        entry=bias+f['old_vaddr'];target=bias+f['new_vaddr']
        if -(1<<27)<=target-entry<(1<<27):entry_targets[entry]=(target,None)
        else:far.append(f)
    scores={}
    profile=report['build'].get('fdata_path')
    if profile and Path(profile).exists():
        for line in Path(profile).read_text().splitlines():
            fields=line.split()
            if len(fields)==8 and fields[3]=='1' and fields[5]=='0':
                key=profile_function(fields[4]);scores[key]=scores.get(key,0)+int(fields[7])
    ordered=sorted(far,key=lambda f:(not bool(profile_score(scores,f['name'],f['old_vaddr'])),f['old_vaddr']))
    if cmd.get('hot_entries_only'):
        ordered=[f for f in ordered if profile_score(scores,f['name'],f['old_vaddr'])>0]
        hot_addresses={bias+f['old_vaddr'] for f in functions if profile_score(scores,f['name'],f['old_vaddr'])>0}
        entry_targets={entry:value for entry,value in entry_targets.items() if entry in hot_addresses}
    for offset in range(0,len(ordered),256):
        group=ordered[offset:offset+256];lo=bias+min(f['old_vaddr'] for f in group);hi=bias+max(f['old_vaddr'] for f in group)
        chosen=None
        for a,b in sorted(gaps(max(65536,hi-(1<<27)+65536),min((1<<48)-65536,lo+(1<<27)-65536)),key=lambda p:abs(p[0]-lo)):
            address=(a+4095)&~4095
            if address+4096<=b and w.helper.rb_reserve(address,4096):chosen=address;break
        if chosen is None:
            rejected.extend({'name':f['name'],'old_vaddr':f['old_vaddr'],'reason':'本组没有可用近端跳板页'} for f in group);continue
        islands.append(chosen);pages[chosen]={'raw':bytearray(struct.pack('<I',0xd4200000)*1024),'executable':True}
        for i,f in enumerate(group):
            entry=bias+f['old_vaddr'];target=bias+f['new_vaddr'];island=chosen+i*16
            pages[chosen]['raw'][i*16:i*16+12]=veneer(island,target);entry_targets[entry]=(island,island)
    patches=[]
    for f in functions:
        entry=bias+f['old_vaddr'];target=bias+f['new_vaddr']
        if entry not in entry_targets:continue
        branch_target,island=entry_targets[entry]
        offset=next(s['offset']+f['old_vaddr']-s['addr'] for s in old.sections if s['flags']&4 and s['addr']<=f['old_vaddr']<s['addr']+s['size'])
        expected=old.data[offset:offset+4]
        if ctypes.string_at(entry,4)!=expected:raise RuntimeError('原入口已改变:'+f['name'])
        replacement=struct.pack('<I',branch_word('B',entry,branch_target))
        patches.append({'address':entry,'old':expected.hex(),'new':replacement.hex(),'target':target,'island':island,'name':f['name'],'old_size':f['old_size']})
    for page,p in pages.items():
        if w.helper.rb_prepare(page,4096):raise OSError(ctypes.get_errno(),'新页准备失败')
        ctypes.memmove(page,bytes(p['raw']),4096)
        if w.helper.rb_publish(page,4096):raise OSError(ctypes.get_errno(),'新页发布失败')
        if not p['executable']:
            libc=ctypes.CDLL(None,use_errno=True);libc.mprotect.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
            if libc.mprotect(page,4096,1):raise OSError(ctypes.get_errno(),'新元数据只读失败')
    # 单个FDE对象插进已有大对象的地址空洞，会破坏libgcc对象范围查询。
    # 所有新函数CFI合并为一个对象，pcrel字段逐项重定位。
    frame_address=(bias+max(s['addr']+s['size'] for s in report['new_sections'])+8191)&~4095
    new_code_ranges=[(bias+s['addr'],bias+s['addr']+s['size']) for s in report['new_sections'] if s['flags']&4]
    frame_blob,frame_report=relocate(new.blob(new.by_name['.eh_frame']),bias+new.by_name['.eh_frame']['addr'],frame_address,new_code_ranges)
    starts={r['start'] for r in frame_report['ranges']};supplemented=[]
    for f in functions:
        if f['name']=='call_weak_fn' and bias+f['new_vaddr'] not in starts:supplemented.extend(supplement_weak(w,old,new,f,bias))
    frame_blob=frame_blob[:-4]+b''.join(bytes.fromhex(p['frame_hex'])[:-4] for p in supplemented)+bytes(4)
    frame_size=(len(frame_blob)+4095)&~4095
    if frame_address+frame_size>reservation['address']+reservation['size']:raise RuntimeError('合并CFI超出保留区')
    if w.helper.rb_prepare(frame_address,frame_size):raise OSError(ctypes.get_errno(),'合并CFI准备失败')
    ctypes.memmove(frame_address,frame_blob,len(frame_blob))
    libc=ctypes.CDLL(None,use_errno=True);libc.mprotect.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
    if libc.mprotect(frame_address,frame_size,1):raise OSError(ctypes.get_errno(),'合并CFI只读发布失败')
    regions=[];pending=None
    def finish_region():
        if pending is None:return
        raw=b''.join(bytes(pages[p]['raw']) for p in pending['pages'])
        if ctypes.string_at(pending['address'],len(raw))!=raw:raise RuntimeError('新页发布后读回不符')
        regions.append({'address':pending['address'],'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'permissions':pending['permissions']})
    for page,p in sorted(pages.items()):
        permissions='rx' if p['executable'] else 'r'
        if pending and pending['address']+len(pending['pages'])*4096==page and pending['permissions']==permissions and len(pending['pages'])<256:
            pending['pages'].append(page)
        else:
            finish_region();pending={'address':page,'pages':[page],'permissions':permissions}
    finish_region()
    expected_frame=frame_blob+bytes(frame_size-len(frame_blob))
    if ctypes.string_at(frame_address,frame_size)!=expected_frame:raise RuntimeError('合并CFI读回不符')
    regions.append({'address':frame_address,'size':frame_size,'sha256':hashlib.sha256(expected_frame).hexdigest(),'permissions':'r'})
    w.helper.rb_register_frame_at.argtypes=[ctypes.c_uint64]
    w.helper.rb_register_frame_at(frame_address)
    for page in islands:
        frame=leaf_frame(page,4096);handle=w.helper.rb_register_frame(frame,len(frame))
        if not handle:raise OSError(ctypes.get_errno(),'跳板CFI登记失败')
        w.frame_handles.append(handle)
        if w.helper.rb_find_frame(page)!=page:raise RuntimeError('跳板CFI核验失败')
    frame_checks=[]
    for f in functions:
        target=bias+f['new_vaddr'];actual=w.helper.rb_find_frame(target)
        original=w.helper.rb_find_frame(bias+f['old_vaddr'])
        if actual!=target and original:raise RuntimeError('新CFI入口不符:'+str((f['name'],hex(target),hex(actual),hex(original))))
        frame_checks.append({'entry':target,'found':actual,'original_found':original,'coverage_preserved':bool(actual)==bool(original)})
    sync=w.helper.rb_sync_threads()
    if sync<0:raise OSError(ctypes.get_errno(),'线程ISB同步失败')
    result={'name':name,'patches':patches,'function_count':len(patches),'load_bias':bias,
        'new_pages':len(pages),'entry_island_pages':islands,'frame_checks':frame_checks,
        'new_regions':regions,
        'original_function_count':report['original_function_count'],'generated_function_count':report['function_count'],
        'entry_candidate_count':len(functions),'unmapped_function_count':len(report['rejected']),
        'entry_rejections':rejected,'direct_entries':sum(p['island'] is None for p in patches),
        'private_readonly_tables':[{k:v for k,v in t.items() if k not in ('old_values','new_values')} for t in report.get('private_readonly_tables',[])],
        'supplemented_leaf_cfi':supplemented,
        'combined_cfi':{'address':frame_address,'bytes':len(frame_blob),'fdes':frame_report['fdes'],'relocated_fields':len(frame_report['relocated_fields']),'supplemented':len(supplemented),'ignored_original_fdes':frame_report['ignored_original_fdes']},
        'isb_other_threads':sync,'proof':proof,'output_sha256':sha(output),'online_module_sha256':sha(__file__),
        'loader_sha256':sha(w.helper_path)}
    result['thread_sync']={'method':'membarrier-private-expedited-sync-core' if w.helper.rb_sync_method() else 'realtime-signal-isb',
        'observed_threads':len(list(Path('/proc/self/task').iterdir()))}
    result['verified_now_bindings']=len(bindings)
    used_end=max([r['address']+r['size'] for r in regions])
    release_start=(used_end+4095)&~4095
    release_size=reservation['address']+reservation['size']-release_start
    if release_size>0:
        libc.munmap.argtypes=[ctypes.c_void_p,ctypes.c_size_t]
        if libc.munmap(release_start,release_size):raise OSError(ctypes.get_errno(),'释放未使用预留尾部失败')
    result['unused_reservation_released_bytes']=max(0,release_size)
    atomic_json(w.out/('loaded-'+name+'.json'),result)
    return result
