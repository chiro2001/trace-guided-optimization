"""Small, dependency-free ELF64 reader. All profile addresses are ELF virtual addresses."""
from pathlib import Path
import struct
import hashlib
import shutil
import os
import stat
from .util import sha256


class ELF:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._fd=os.open(self.path,os.O_RDONLY|os.O_NONBLOCK)
        opened=os.fstat(self._fd)
        if not stat.S_ISREG(opened.st_mode):raise ValueError('ELF input must be a regular file')
        self._identity=self._stamp(opened)
        self._size=opened.st_size;self._data=None
        if self._size < 64:
            raise ValueError(f"requires little-endian ELF64: {path}")
        header=self.read(0,64)
        if header[:6] != b"\x7fELF\x02\x01":raise ValueError(f"requires little-endian ELF64: {path}")
        h = list(struct.unpack("<16sHHIQQQIHHHHHH", header))
        if h[8] != 64:
            raise ValueError("invalid ELF64 header size")
        if h[6]:
            self._bounds(h[6], 64, "section zero")
            zero = struct.unpack("<IIQQQQIIQQ", self.read(h[6],64))
            if h[12] == 0: h[12] = zero[5]
            if h[13] == 0xffff: h[13] = zero[6]
            if h[10] == 0xffff: h[10] = zero[7]
        elif h[12] or h[13]:
            raise ValueError("section header table missing")
        if h[10] and h[9] < 56:
            raise ValueError("invalid ELF64 program header size")
        if h[12] and h[11] < 64:
            raise ValueError("invalid ELF64 section header size")
        self._bounds(h[5], h[9] * h[10], "program header table")
        self._bounds(h[6], h[11] * h[12], "section header table")
        self.header = tuple(h)
        self.type, self.machine = h[1:3]
        self.entry = h[4]
        self.loads = []
        self.interpreter = None
        self.dynamic_tags = set()
        self.dynamic_values = {}
        program_headers=self.read(h[5],h[9]*h[10])
        for i in range(h[10]):
            typ, flags, off, va, _, size, memsize, align = struct.unpack_from(
                "<IIQQQQQQ", program_headers, i * h[9])
            self._bounds(off, size, "program segment")
            if typ == 1:
                if size > memsize or (align > 1 and (align & (align-1) or off % align != va % align)):
                    raise ValueError("invalid PT_LOAD size or alignment")
                self.loads.append((va, va + memsize, off, flags, size))
            if typ == 3:
                self.interpreter = self.read(off,size).rstrip(b"\0").decode()
            if typ == 2:
                if size % 16:
                    raise ValueError("invalid ELF64 dynamic segment")
                dynamic=self.read(off,size)
                for pos in range(0,size,16):
                    tag, value = struct.unpack_from("<qQ", dynamic, pos)
                    if tag == 0:
                        break
                    self.dynamic_tags.add(tag)
                    self.dynamic_values[tag] = value
        section_headers=self.read(h[6],h[11]*h[12])
        sections = [struct.unpack_from("<IIQQQQIIQQ", section_headers, i * h[11])
                    for i in range(h[12])]
        self.section_entries = sections
        if sections and h[13] >= len(sections):
            raise ValueError("ELF section name table missing")
        for section in sections:
            if section[1] != 8:
                self._bounds(section[4], section[5], "section payload")
        s = sections[h[13]] if sections else None
        names = self.read(s[4],s[5]) if s else b""
        self.sections = {}
        self.build_id = None
        for s in sections:
            if s[0] >= len(names) or b'\0' not in names[s[0]:]:
                raise ValueError("invalid section name offset or terminator")
            name = names[s[0]:].split(b"\0", 1)[0].decode(errors="replace")
            self.sections[name] = s
            if name == ".note.gnu.build-id":
                note=self.read(s[4],s[5]);off,end=0,len(note)
                while off + 12 <= end:
                    n, d, typ = struct.unpack_from("<III", note, off)
                    desc = off + 12 + ((n + 3) & ~3)
                    if desc+d>end:raise ValueError('build-id descriptor outside ELF note')
                    if typ == 3 and note[off + 12:off + 15] == b"GNU":
                        self.build_id = note[desc:desc + d].hex()
                    off = desc + ((d + 3) & ~3)
        self.assert_unchanged()

    @staticmethod
    def _stamp(st):return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

    def assert_unchanged(self):
        if self._stamp(os.fstat(self._fd))!=self._identity or self._stamp(self.path.stat())!=self._identity:
            raise ValueError('ELF changed while being inspected')

    def _bounds(self, offset, size, label):
        if offset < 0 or size < 0 or offset + size > self._size:
            raise ValueError(f"{label} outside ELF file")

    def read(self,offset,size):
        """元数据按范围读取，诊断大ELF时不把非装载DWARF全部读进内存。"""
        self._bounds(offset,size,'read range')
        if self._data is not None:return self._data[offset:offset+size]
        parts=[];remaining=size
        while remaining:
            block=os.pread(self._fd,min(remaining,16*1024*1024),offset)
            if not block:raise ValueError('ELF changed or truncated during read')
            parts.append(block);offset+=len(block);remaining-=len(block)
        return b''.join(parts)

    @property
    def data(self):
        # 保留既有完整bytes接口；只有实际需要指令或全文件载荷的路径才读取。
        if self._data is None:
            self.assert_unchanged()
            with os.fdopen(os.dup(self._fd),'rb') as stream:
                stream.seek(0);data=stream.read(self._size)
            if len(data)!=self._size:raise ValueError('ELF changed or truncated during read')
            self.assert_unchanged();self._data=data
        return self._data

    def __del__(self):
        descriptor=getattr(self,'_fd',None)
        if descriptor is not None:
            try:os.close(descriptor)
            except OSError:pass
            self._fd=None

    def iter_symbols(self):
        """Yield defined and undefined symbols without relying on filename or digest."""
        for section in self.section_entries:
            if section[1] not in (2, 11):
                continue
            if section[9] != 24 or section[5] % 24 or section[6] >= len(self.section_entries):
                raise ValueError("invalid ELF64 symbol table")
            strings = self.section_entries[section[6]]
            if strings[1] != 3:
                raise ValueError("symbol names do not reference a string table")
            table = self.read(strings[4],strings[5]);symbols=self.read(section[4],section[5])
            for offset in range(0,section[5],24):
                item = struct.unpack_from('<IBBHQQ', symbols, offset)
                end = table.find(b'\0', item[0])
                if item[0] >= len(table) or end < 0:
                    raise ValueError("invalid symbol string")
                yield {'name':table[item[0]:end].decode(errors='replace'), 'type':item[1]&15,
                       'binding':item[1]>>4, 'shndx':item[3], 'value':item[4], 'size':item[5]}

    def contains(self, va):
        return any(lo <= va < hi and flags & 1 for lo, hi, _, flags, _ in self.loads)

    def code_relocation_sections(self):
        """静态重定位由sh_info关联到已装载代码节，不依赖.text等固定节名。"""
        if self.type not in (2,3):return []
        result=[]
        for name,section in self.sections.items():
            width={4:24,9:16}.get(section[1])
            if width is None or section[2]&2 or not section[5] or section[9]!=width or section[5]%width:continue
            index=section[7]
            if 0<index<len(self.section_entries) and self.section_entries[index][2]&6==6:result.append(name)
        return result

    def symbol(self, name):
        matches = {(s['shndx'],s['value'],s['size']) for s in self.iter_symbols()
                   if s['name']==name and s['shndx']!=0}
        if len(matches) != 1:
            raise ValueError(f'missing or ambiguous ELF symbol: {name}')
        return matches.pop()

    def metadata(self):
        self.assert_unchanged()
        st = self.path.stat()
        go=any('gopclntab' in name or name=='.go.buildinfo' for name in self.sections)
        if not go:go=any(s['shndx'] and s['name'] in ('runtime.firstmoduledata','runtime.pclntab') for s in self.iter_symbols())
        digest=sha256(self.path);self.assert_unchanged()
        return {"path": str(self.path), "sha256": digest, "build_id": self.build_id,
                "machine": self.machine, "type": self.type, "bytes": st.st_size,
                "relocations": bool(self.code_relocation_sections()),
                "symbols": ".symtab" in self.sections, "go":go,
                "interpreter": self.interpreter,
                "entry": self.entry,
                "pie": bool(self.dynamic_values.get(0x6ffffffb, 0) & 0x08000000),
                "rpath_kind": "rpath" if 15 in self.dynamic_tags else "runpath" if 29 in self.dynamic_tags else None}


def is_executable_image(row):
    if row["type"] == 2 or row["interpreter"]:
        return True
    if "pie" in row:
        return row["pie"]
    if row.get("entry", 0):
        # Older captures did not record DF_1_PIE. A nonzero e_entry is also
        # valid for dlopen-able DSOs (including NumPy's core extension).
        try:
            return bool(ELF(row["path"]).dynamic_values.get(0x6ffffffb, 0) & 0x08000000)
        except (OSError, ValueError, KeyError):
            return True
    return False


def payload_hash(path,table_start,table_end):
    h=hashlib.sha256()
    with path.open("rb") as f:
        remaining=table_start
        while remaining:
            block=f.read(min(1024*1024,remaining))
            if not block:raise ValueError('ELF truncated while hashing payload')
            h.update(block);remaining-=len(block)
        f.seek(table_end)
        for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
    return h.hexdigest()


def normalize_loads(source,target,page_size=None):
    page_size=page_size or os.sysconf('SC_PAGE_SIZE')
    if page_size<=0 or page_size&(page_size-1):raise ValueError('invalid target page size')
    with source.open("rb") as f:
        h=struct.unpack("<16sHHIQQQIHHHHHH",f.read(64))
        if h[0][:6]!=b"\x7fELF\x02\x01" or h[1] not in (2,3) or h[2] not in (62,183):
            raise ValueError("expected supported little-endian executable/shared ELF64")
        phoff,phsize,phnum=h[5],h[9],h[10]
        if phsize<56 or phoff+phsize*phnum>source.stat().st_size:raise ValueError("invalid program headers")
        f.seek(phoff);table=f.read(phsize*phnum)
        entries=[table[i*phsize:(i+1)*phsize] for i in range(phnum)]
        values=[struct.unpack_from("<IIQQQQQQ",entry) for entry in entries]
        slots=[i for i,v in enumerate(values) if v[0]==1]
        loads=[values[i] for i in slots]
        alignment_changes=[]
        for slot,v in zip(slots,loads):
            typ,flags,offset,va,pa,fs,ms,align=v
            if fs>ms or offset+fs>source.stat().st_size:raise ValueError("invalid PT_LOAD size")
            if align>1 and align&(align-1):raise ValueError("non-power-of-two PT_LOAD alignment")
            if va%page_size!=offset%page_size:raise ValueError('PT_LOAD cannot map at target page size')
            if align>1 and va%align!=offset%align:
                delta=va-offset;compatible=min(align,delta&-delta)
                if compatible<page_size:raise ValueError('no compatible PT_LOAD alignment')
                entry=bytearray(entries[slot]);struct.pack_into('<Q',entry,48,compatible);entries[slot]=bytes(entry)
                alignment_changes.append({'slot':slot,'vaddr':va,'offset':offset,'before':align,'after':compatible})
        ordered=sorted((entries[i] for i in slots),key=lambda e:struct.unpack_from("<Q",e,16)[0])
        ordered_values=[struct.unpack_from("<IIQQQQQQ",e) for e in ordered]
        if any(a[3]+a[6]>b[3] for a,b in zip(ordered_values,ordered_values[1:])):
            raise ValueError("overlapping virtual PT_LOAD intervals")
        # Program-header changes must not overwrite an executable section.
        for i in range(h[12]):
            f.seek(h[6]+i*h[11]);s=struct.unpack("<IIQQQQIIQQ",f.read(64))
            if s[2]&4 and max(s[4],phoff)<min(s[4]+s[5],phoff+len(table)):
                raise ValueError("program headers overlap instructions")
    for i,entry in zip(slots,ordered):entries[i]=entry
    new_table=b"".join(entries)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,target)
    with target.open("r+b") as f:f.seek(phoff);f.write(new_table)
    before=payload_hash(source,phoff,phoff+len(table))
    after=payload_hash(target,phoff,phoff+len(table))
    if before!=after:raise ValueError("payload changed")
    return {"changed":new_table!=table,"program_header_offset":phoff,'target_page_size':page_size,
            'alignment_changes':alignment_changes,
            "before_load_vaddrs":[v[3] for v in loads],"after_load_vaddrs":[v[3] for v in ordered_values],
            "all_bytes_outside_program_headers_sha256":before,"code_and_data_payload_unchanged":True}

