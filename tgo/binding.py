"""Enable eager ELF binding without modifying executable instructions."""
from pathlib import Path
import shutil
import struct
from .elf import ELF
from .util import sha256,write_json,IdentityError


def bind_now(source, target):
    source,target=Path(source),Path(target)
    if target.exists():raise FileExistsError(target)
    elf=ELF(source); original=elf.data;raw=bytearray(original)
    if elf.type!=3 or elf.machine!=183:raise ValueError('BIND_NOW recipe requires an AArch64 DSO')
    header=struct.unpack_from('<16sHHIQQQIHHHHHH',raw)
    tables=[]
    for i in range(header[10]):
        p=struct.unpack_from('<IIQQQQQQ',raw,header[5]+i*header[9])
        if p[0]==2:tables.append((p[2],p[5]))
    if len(tables)!=1:raise ValueError('ambiguous ELF dynamic table')
    start,size=tables[0];entries=[];terminator=None
    for offset in range(start,start+size,16):
        tag,value=struct.unpack_from('<QQ',raw,offset)
        if tag==0:terminator=offset;break
        entries.append((offset,tag,value))
    chosen=next((e for e in entries if e[1]==30),None)
    if chosen:
        offset,tag,old=chosen;new=(tag,old|8)
    else:
        chosen=next((e for e in entries if e[1]==0x6ffffffb),None)
        if chosen:
            offset,tag,old=chosen;new=(tag,old|1)
        else:
            chosen=next((e for e in entries if e[1]==0x6ffffff9),None)
            if chosen:offset,tag,old=chosen
            elif terminator is not None and terminator+32<=start+size and struct.unpack_from('<Q',raw,terminator+16)[0]==0:
                offset,tag,old=terminator,0,0
            else:raise ValueError('no safe dynamic entry available for BIND_NOW')
            new=(30,8)
    for section in elf.sections.values():
        if section[2]&4 and max(section[4],offset)<min(section[4]+section[5],offset+16):
            raise ValueError('dynamic entry overlaps executable instructions')
    struct.pack_into('<QQ',raw,offset,*new)
    if raw[:offset]!=original[:offset] or raw[offset+16:]!=original[offset+16:]:
        raise IdentityError('BIND_NOW mutation escaped its dynamic entry')
    visible=False
    for pos in range(start,start+size,16):
        t,v=struct.unpack_from('<QQ',raw,pos)
        if t==0:break
        visible |= t==24 or t==30 and bool(v&8) or t==0x6ffffffb and bool(v&1)
    if not visible:raise ValueError('BIND_NOW not visible to loader')
    target.write_bytes(raw);shutil.copymode(source,target)
    record={'source':str(source),'source_sha256':sha256(source),'output_sha256':sha256(target),
        'dynamic_entry_offset':offset,'old_entry':[tag,old],'new_entry':list(new),
        'executable_instructions_unchanged':True,'all_other_bytes_unchanged':True}
    write_json(target.with_name(target.name+'.binding.json'),record)
    return record
