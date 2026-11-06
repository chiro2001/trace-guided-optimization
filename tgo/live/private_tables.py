#!/usr/bin/env python3
"""CPython静态opcode跳转表逐版本复制；原表供旧帧使用。"""
import struct,re,subprocess
from .common import *
from tgo.util import tool,tool_env

def sx(v,bits):return v-(1<<bits) if v&(1<<(bits-1)) else v
def bytes_at(elf,address,size):
    s=next(s for s in elf.sections if s['type']!=8 and s['addr']<=address and address+size<=s['addr']+s['size'])
    offset=s['offset']+address-s['addr'];return elf.data[offset:offset+size]
def relative_slots(elf):
    result={}
    for s in elf.sections:
        if s['type']==4 and s['flags']&2:
            for offset,info,addend in struct.iter_unpack('<QQq',elf.blob(s)):
                if info&0xffffffff==1027:result[offset]=addend
    return result

def describe(old,new,report,vma):
    symbols={}
    for symbol in old.symbols:
        if symbol['type']==1 and symbol['binding']==0 and symbol['name'].startswith(('opcode_targets.','sre_targets.')):
            symbols[(symbol['value'],symbol['size'])]=symbol
    if not any(s['name'].startswith('opcode_targets.') for s in symbols.values()):raise RuntimeError('缺少opcode表')
    old_relas,new_relas=relative_slots(old),relative_slots(new)
    branch_targets={s['value'] for s in new.symbols if s['type']==2 and s['shndx']}
    for section in report['new_sections']:
        if not section['flags']&4:continue
        for i,(word,) in enumerate(struct.iter_unpack('<I',new.blob(section))):
            pc=section['addr']+i*4
            if word&0x7c000000==0x14000000:branch_targets.add(pc+sx(word&0x3ffffff,26)*4)
            elif word&0xff000010==0x54000000 or word&0x7e000000==0x34000000:branch_targets.add(pc+sx((word>>5)&0x7ffff,19)*4)
            elif word&0x7e000000==0x36000000:branch_targets.add(pc+sx((word>>5)&0x3fff,14)*4)
    tables=[];patches=[]
    for table_index,((address,size),symbol) in enumerate(sorted(symbols.items())):
        if size%8 or not 0<size<=2048:raise RuntimeError('不是受支持的局部只读标签表')
        section=next(s for s in old.sections if s['addr']<=address and address+size<=s['addr']+s['size'])
        if section['name']!='.data.rel.ro':raise RuntimeError('标签表不在RELRO节')
        old_values=[];new_values=[]
        for i in range(size//8):
            slot=address+i*8
            a=old_relas.get(slot,int.from_bytes(bytes_at(old,slot,8),'little'))
            b=new_relas.get(slot,int.from_bytes(bytes_at(new,slot,8),'little'))
            if a%4 or not any(s['flags']&4 and s['addr']<=a<s['addr']+s['size'] for s in old.sections):raise RuntimeError('旧标签不在代码中')
            old_values.append(a);new_values.append(b);branch_targets.add(b)
        owners={(s['value'],s['size']):s['name'] for s in old.symbols if s['type']==2 and s['size'] and all(s['value']<=a<s['value']+s['size'] for a in old_values)}
        if len(owners)!=1:raise RuntimeError('只读标签表的函数所有者不唯一:'+symbol['name'])
        (owner,owner_size),owner_name=next(iter(owners.items()))
        mapped=[f for f in report['functions'] if f['old_vaddr']==owner]
        if not mapped and old_values==new_values:continue
        if len(mapped)!=1:raise RuntimeError('标签表所有者未迁移')
        if not all(any(s['flags']&4 and s['addr']<=b<s['addr']+s['size'] for s in report['new_sections']) for b in new_values):raise RuntimeError('新标签不在BOLT代码中')
        owner_ranges={(mapped[0]['new_vaddr'],mapped[0]['new_vaddr']+mapped[0]['new_size'])}
        names={s['name'] for s in old.symbols if s['type']==2 and s['value']==owner}
        for ns in new.symbols:
            if ns['type']==2 and ns['size'] and any(ns['name'].startswith(name+'.cold') for name in names):owner_ranges.add((ns['value'],ns['value']+ns['size']))
        code_start=min(s['addr'] for s in report['new_sections'] if s['flags']&4)
        code_end=max(s['addr']+s['size'] for s in report['new_sections'] if s['flags']&4)
        destination_options=[code_start-65536+table_index*4096,((code_end+4095)&~4095)+4096+table_index*4096]
        instruction_text={}
        dis_names={mapped[0]['name']}
        dis_names.update(ns['name'] for ns in new.symbols if ns['type']==2 and ns['size'] and any(ns['name'].startswith(name+'.cold') for name in names))
        for dis_name in dis_names:
            text=subprocess.check_output([tool('objdump'),'-d','--no-show-raw-insn','--disassemble='+dis_name,str(new.path)],text=True,env=tool_env())
            for line in text.splitlines():
                match=re.match(r'\s*([0-9a-f]+):\s+([a-z0-9.]+)\s*(.*)',line)
                if match:instruction_text[int(match[1],16)]=(match[2],match[3])
        words={}
        for sec in report['new_sections']:
            if not sec['flags']&4:continue
            for i,(word,) in enumerate(struct.iter_unpack('<I',new.blob(sec))):
                pc=sec['addr']+i*4
                if any(lo<=pc<hi for lo,hi in owner_ranges):words[pc]=word
        from collections import deque
        entry=mapped[0]['new_vaddr'];initial=[None]*32;initial[31]=('stack',0);states={entry:(tuple(initial),())};queue=deque([entry]);candidates={};unresolved=set();load_sources={}
        labels=('labels',tuple(sorted(set(new_values))))
        noreturn_names={'_Py_FatalErrorFunc','_Py_FatalErrorFormat','Py_FatalError','Py_Exit','abort','__assert_fail','__stack_chk_fail','__cxa_throw','_Unwind_Resume'}
        noreturn_targets={sym['value'] for sym in new.symbols if sym['shndx'] and canonical_function(sym['name']) in noreturn_names}
        original_plt,_=old.ordinary_plt()
        noreturn_targets.update(pc for pc,item in original_plt.items() if item['symbol']['name'] in noreturn_names)
        all_code={}
        for section in report['new_sections']:
            if section['flags']&4:
                all_code.update({section['addr']+i*4:w for i,(w,) in enumerate(struct.iter_unpack('<I',new.blob(section)))})
        for stub_pc,a in all_code.items():
            d,b=all_code.get(stub_pc+4,0),all_code.get(stub_pc+8,0)
            if a&0x9f00001f==0x90000010 and d&0xffc003ff==0x91000210 and b==0xd61f0200:
                target=(stub_pc&~4095)+sx(((a>>29)&3)|(((a>>5)&0x7ffff)<<2),21)*4096+((d>>10)&4095)
                if target in noreturn_targets:noreturn_targets.add(stub_pc)
        def add_edge(pc,values,memory):
            if pc not in words:return
            old_state=states.get(pc)
            if old_state is None:merged=(tuple(values),tuple(sorted(memory.items())))
            else:
                old_regs,old_mem=old_state;old_mem=dict(old_mem)
                merged=(tuple(a if a==b else None for a,b in zip(old_regs,values)),tuple(sorted((k,v) for k,v in memory.items() if old_mem.get(k)==v)))
            if old_state!=merged:states[pc]=merged;queue.append(pc)
        def memory_address(operands,state):
            match=re.search(r'\[(sp|x[0-9]+)(?:,\s*#(-?(?:0x[0-9a-f]+|[0-9]+)))?\]',operands)
            if not match:return None,None,None
            reg=31 if match[1]=='sp' else int(match[1][1:]);base=state[reg]
            if not isinstance(base,tuple) or base[0]!='stack':return None,None,None
            displacement=int(match[2],0) if match[2] else 0
            address=base[1]+displacement
            update=address if '!' in operands else None
            post=re.search(r'\],\s*#(-?(?:0x[0-9a-f]+|[0-9]+))',operands)
            if post:address=base[1];update=base[1]+int(post[1],0)
            return address,reg,update
        def write_slot(memory,address,size,value):
            for key in list(memory):
                if max(key,address)<min(key+8,address+size):memory.pop(key,None)
            if size==8 and value is not None:memory[address]=value
        while queue:
            pc=queue.popleft();word=words[pc];state=list(states[pc][0]);memory=dict(states[pc][1]);rd=word&31;rn=(word>>5)&31
            mnemonic,operands=instruction_text.get(pc,('', ''));next_pc=pc+4
            if word&0x9f000000==0x90000000 and rd<31:
                state[rd]=(pc&~4095)+sx(((word>>29)&3)|(((word>>5)&0x7ffff)<<2),21)*4096
            elif word&0x9f000000==0x10000000 and rd<31:
                state[rd]=pc+sx(((word>>29)&3)|(((word>>5)&0x7ffff)<<2),21)
            elif word&0x1f000000==0x11000000:
                immediate=((word>>10)&4095)<<(12 if word&(1<<22) else 0)
                base=state[rn]
                if isinstance(base,tuple) and base[0]=='stack' and word>>31:value=('stack',base[1]+(-immediate if word&(1<<30) else immediate))
                else:value=None if not isinstance(base,int) or not word>>31 else (base+(-immediate if word&(1<<30) else immediate))&((1<<64)-1)
                if word>>31 and not word&(1<<29) and rd<29 and value==address:candidates[pc]=(word,rd)
                else:candidates.pop(pc,None)
                if rd<31 or not word&(1<<29):state[rd]=value
            elif word&0xffe00c00==0xf8600800:
                base=state[rn] if rn<31 else None
                if rd<31:state[rd]=labels if base==address else None
                load_sources[pc]=(rn,base,'indexed')
            elif word&0xffc00000==0xf9400000:
                base=state[rn];effective=base+((word>>10)&4095)*8 if isinstance(base,int) else None
                if isinstance(base,tuple) and base[0]=='stack':value=memory.get(base[1]+((word>>10)&4095)*8)
                else:value=('labels',(new_values[(effective-address)//8],)) if effective is not None and address<=effective<address+size and not (effective-address)%8 else None
                if rd<31:state[rd]=value
                load_sources[pc]=(rn,base,'immediate')
            elif word&0xfffffc1f==0xd65f0000 or word&0xffe0001f==0xd4200000 or word&0xffe0001f==0xd4400000:
                continue
            elif word&0xfffffc1f==0xd61f0000:
                target=state[rn] if rn<31 else None
                if isinstance(target,tuple) and target[0]=='labels':
                    for successor in target[1]:add_edge(successor,state,memory)
                    unresolved.discard(pc)
                elif isinstance(target,int) and target in words:add_edge(target,state,memory)
                else:
                    # 普通GOT尾跳退出本函数；无法确认的寄存器索引分派拒绝。
                    prior=load_sources.get(pc-4)
                    if state[31]!=('stack',0):unresolved.add(pc)
                continue
            elif word&0xfc000000==0x14000000:
                add_edge(pc+sx(word&0x3ffffff,26)*4,state,memory);continue
            elif word&0xfc000000==0x94000000 or word&0xfffffc1f==0xd63f0000:
                if word&0xfc000000==0x94000000 and pc+sx(word&0x3ffffff,26)*4 in noreturn_targets:continue
                for reg in [*range(19),30]:state[reg]=None
            elif word&0xff000010==0x54000000 or word&0x7e000000==0x34000000:
                add_edge(pc+sx((word>>5)&0x7ffff,19)*4,state,memory)
            elif word&0x7e000000==0x36000000:
                add_edge(pc+sx((word>>5)&0x3fff,14)*4,state,memory)
            elif mnemonic=='mov':
                regs=re.findall(r'\b([xw])([0-9]+)\b',operands)
                if len(regs)==2 and int(regs[0][1])<31:
                    state[int(regs[0][1])]=state[int(regs[1][1])] if int(regs[1][1])<31 and regs[0][0]=='x' else None
                elif rd<31:state[rd]=None
            elif mnemonic.startswith(('str','stur','stp','stnp','stlr')):
                location,base_reg,update=memory_address(operands,state)
                if location is not None:
                    before=operands.split('[',1)[0]
                    regs=re.findall(r'\b([xwqds])([0-9]+|zr)\b',before)
                    if not regs:
                        write_slot(memory,location,16,None)
                    else:
                        step={'x':8,'w':4,'q':16,'d':8,'s':4}[regs[0][0]]
                        if mnemonic.startswith(('strb','sturb')):step=1
                        if mnemonic.startswith(('strh','sturh')):step=2
                        for index,(kind,reg) in enumerate(regs):
                            value=state[int(reg)] if kind=='x' and reg!='zr' and int(reg)<31 else None
                            write_slot(memory,location+index*step,step,value)
                    if update is not None:state[base_reg]=('stack',update)
            elif mnemonic.startswith(('ldp','ldnp','ldur','ldr')):
                location,base_reg,update=memory_address(operands,state)
                regs=re.findall(r'\b([xw])([0-9]+)\b',operands.split('[',1)[0])
                for index,(kind,reg) in enumerate(regs):
                    value=memory.get(location+index*8) if location is not None and kind=='x' else None
                    if int(reg)<31:state[int(reg)]=value
                if update is not None:state[base_reg]=('stack',update)
            elif mnemonic.startswith(('cmp','cmn','tst','ccmp','ccmn','fcmp')) or mnemonic in ('nop','bti','msr'):
                pass
            elif mnemonic.startswith(('cas','swp','ldadd','ldclr','ldeor','ldset','ldsmax','ldsmin','ldumax','ldumin')):
                for reg in re.findall(r'\b[xw]([0-9]+)\b',operands):
                    if int(reg)<31:state[int(reg)]=None
            elif mnemonic:
                first=operands.split(',',1)[0].strip();match=re.fullmatch(r'[xw]([0-9]+)',first)
                if match and int(match[1])<31:state[int(match[1])]=None
                if mnemonic.startswith(('ldp','ldnp')):
                    regs=re.findall(r'\b[xw]([0-9]+)\b',operands.split('[',1)[0])
                    for reg in regs:
                        if int(reg)<31:state[int(reg)]=None
                if '!' in operands or re.search(r'\],\s*#',operands):
                    match=re.search(r'\[x([0-9]+)',operands)
                    if match and int(match[1])<31:state[int(match[1])]=None
            else:
                state=[None]*32
            add_edge(next_pc,state,memory)
        if unresolved:raise RuntimeError('标签表分派基址未得到完整证明:'+symbol['name']+':'+str([(hex(x),load_sources.get(x-4),states.get(x,([None]*32,()))[0][23],dict(states.get(x,([],()))[1])) for x in sorted(unresolved)[:4]]))
        if not candidates:raise RuntimeError('标签表没有已证明的常量地址计算:'+symbol['name'])
        def producer_for(pc,reg):
            if ((words[pc]>>5)&31)!=reg:return None
            cursor=pc-4
            while cursor>=pc-32 and cursor in words:
                if cursor+4 in branch_targets:return None
                w=words[cursor];mn,ops=instruction_text.get(cursor,('', ''))
                if mn in ('b','br','blr','bl','ret') or mn.startswith(('b.','cb','tb')):return None
                mentions=reg in [int(x) for x in re.findall(r'\b[xw]([0-9]+)\b',ops)]
                if mentions:
                    legal=(w&31)==reg and (w&0x9f000000 in (0x10000000,0x90000000) or w&0x1f000000==0x11000000 and w>>31 and not w&(1<<29) or mn=='mov' and ops.split(',',1)[0].strip()=='x'+str(reg))
                    return cursor if legal else None
                cursor-=4
            return None
        local=[];destinations=set();far_destination=vma+(16<<20)+table_index*4096
        for pc,(word,rd) in sorted(candidates.items()):
            producer=producer_for(pc,rd)
            if producer is not None:
                destination=far_destination;delta=((destination&~4095)-(producer&~4095))//4096
                if not -(1<<20)<=delta<(1<<20):raise RuntimeError('私有标签表ADRP超距')
                encoded=delta&0x1fffff
                adrp=0x90000000|rd|((encoded&3)<<29)|((encoded>>2)<<5)
                add=0x91000000|rd|(rd<<5)|((destination&4095)<<10)
                local.append({'vaddr':producer,'old_hex':struct.pack('<I',words[producer]).hex(),'new_hex':struct.pack('<I',adrp).hex(),
                    'kind':'private-table-ADRP-producer','register':rd,'new_target':destination,'table':symbol['name']})
                local.append({'vaddr':pc,'old_hex':struct.pack('<I',word).hex(),'new_hex':struct.pack('<I',add).hex(),
                    'kind':'private-table-ADD-final','register':rd,'old_target':address,'new_target':destination,'table':symbol['name']})
            else:
                destination=next((p for p in destination_options if -(1<<20)<=p-pc<(1<<20)),None)
                if destination is None:raise RuntimeError('标签表地址既无可复用生产者，也无ADR近区:'+symbol['name']+':'+hex(pc))
                delta=destination-pc;encoded=delta&0x1fffff;replacement=0x10000000|rd|((encoded&3)<<29)|((encoded>>2)<<5)
                decoded=pc+sx(((replacement>>29)&3)|(((replacement>>5)&0x7ffff)<<2),21)
                if decoded!=destination:raise RuntimeError('私有ADR独立解码失败')
                local.append({'vaddr':pc,'old_hex':struct.pack('<I',word).hex(),'new_hex':struct.pack('<I',replacement).hex(),
                    'kind':'private-table-ADR-final','register':rd,'old_target':address,'new_target':destination,'table':symbol['name']})
            destinations.add(destination)
        if not local:raise RuntimeError('标签表没有受支持的地址改写:'+symbol['name'])
        unique={}
        for patch in local:
            if patch['vaddr'] in unique and unique[patch['vaddr']]!=patch:raise RuntimeError('私有表地址改写位点冲突')
            unique[patch['vaddr']]=patch
        local=list(unique.values())
        patches.extend(local)
        for destination in sorted(destinations):
            tables.append({'name':symbol['name'],'owner':owner_name,'old_vaddr':address,'new_vaddr':destination,'size':size,
                'entries':size//8,'old_values':old_values,'new_values':new_values,
                'proof':'局部RELRO标签表；CFG常量传播含私有栈溢出恢复；复用无中间依赖的地址生产者或单条ADR，保持共享section-anchor与其它对象身份'})
    return {'private_readonly_tables':tables,'code_patches':patches,'omit_payload_sections':['.eh_frame','.eh_frame_hdr']}
