"""用符号与同址动态重定位证明入口对应，处理不同编译单元同名局部函数。"""
from collections import defaultdict
import struct

def relative_slots(elf):
    result={}
    for section in elf.sections:
        if section['type']!=4 or not section['flags']&2:continue
        if section['entsize']!=24 or section['size']%24:raise ValueError('动态RELA条目大小无效')
        for offset,info,addend in struct.iter_unpack('<QQq',elf.blob(section)):
            if info!=1027:continue  # R_AARCH64_RELATIVE，symbol index必须为0。
            if offset in result:raise ValueError('同一槽位存在重复RELATIVE重定位')
            result[offset]=addend
    return result

def data_word(elf,address,optional=False):
    regions=[s for s in elf.sections if s['flags']&2 and not s['flags']&4 and s['type'] in (1,14,15,16)
             and s['name'] not in ('.eh_frame','.eh_frame_hdr','.bolt.org.eh_frame','.bolt.org.eh_frame_hdr')
             and s['addr']<=address and address+8<=s['addr']+s['size']]
    if not regions and optional:return None
    if len(regions)!=1 or address%8:raise ValueError('函数指针重定位不在唯一对齐的原数据槽位')
    section=regions[0];offset=address-section['addr']
    raw=elf.blob(section)[offset:offset+8]
    if len(raw)!=8:raise ValueError('函数指针槽位读取不足')
    return int.from_bytes(raw,'little'),section

def functions(old,new,additions):
    originals=defaultdict(list);emitted=defaultdict(list);origins=defaultdict(set)
    for symbol in old.symbols:
        if symbol['type']==2 and symbol['size'] and symbol['shndx'] and symbol['value']:
            originals[symbol['value']].append(symbol)
            origins[symbol['name']].add(symbol['value'])
    for symbol in new.symbols:
        if symbol['type']==2 and symbol['size'] and symbol['shndx'] and any(
                s['flags']&4 and s['addr']<=symbol['value'] and symbol['value']+symbol['size']<=s['addr']+s['size'] for s in additions):
            emitted[symbol['name']].append(symbol)
    candidates={address:{s['value']:s for alias in aliases for s in emitted.get(alias['name'],[])}
                for address,aliases in originals.items()}
    witnesses=defaultdict(list)
    after=relative_slots(new)
    for slot,address in relative_slots(old).items():
        if address not in originals or slot not in after or after[slot]==address:continue
        target=after[slot]
        if target not in candidates[address]:continue
        before=data_word(old,slot,optional=True)
        if before is None:continue
        before_word,before_section=before;after_word,after_section=data_word(new,slot)
        if any(before_section[k]!=after_section[k] for k in ['name','addr','size','flags']):
            raise ValueError('函数指针重定位的原数据形状改变')
        if before_word!=address or after_word!=target:raise ValueError('函数指针值与RELATIVE重定位addend不一致')
        witnesses[address].append({'slot':slot,'old_addend':address,'new_addend':target,
            'kind':'R_AARCH64_RELATIVE','data_words_verified':True})
    mapped=[];rejected=[]
    for address,aliases in sorted(originals.items()):
        targets=set(candidates[address]);observed={w['new_addend'] for w in witnesses[address]}
        strong=set()
        for alias in aliases:
            named={s['value'] for s in emitted.get(alias['name'],[])}
            if origins[alias['name']]=={address} and len(named)==1:strong.update(named)
        if len(observed)>1:raise ValueError('同一原入口的动态重定位对应互相冲突')
        if len(strong)>1:raise ValueError('同一原入口的唯一符号别名对应互相冲突')
        if len(strong)==1:
            chosen=next(iter(strong));basis='unique-symbol-address'
        elif len(observed)==1:
            chosen=next(iter(observed));basis='same-slot-relative-relocation-and-symbol'
        else:
            rejected.append({'old_vaddr':address,'name':aliases[0]['name'],'reason':'未迁移或同名入口缺少唯一重定位证明'});continue
        if observed and observed!={chosen}:raise ValueError('符号与动态重定位入口对应冲突')
        symbol=candidates[address][chosen]
        mapped.append({'old_vaddr':address,'new_vaddr':chosen,'name':symbol['name'],
            'old_size':min(s['size'] for s in aliases),'new_size':symbol['size'],
            'correspondence_basis':basis,'relocation_witnesses':witnesses[address]})
    return mapped,rejected
