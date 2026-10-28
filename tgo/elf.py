"""Small, dependency-free ELF64 reader. All profile addresses are ELF virtual addresses."""
from pathlib import Path
import struct
import hashlib
import shutil
from .util import sha256


class ELF:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        if self.data[:6] != b"\x7fELF\x02\x01":
            raise ValueError(f"requires little-endian ELF64: {path}")
        h = struct.unpack_from("<16sHHIQQQIHHHHHH", self.data)
        self.type, self.machine = h[1:3]
        self.entry = h[4]
        self.loads = []
        self.interpreter = None
        self.dynamic_tags = set()
        for i in range(h[10]):
            typ, flags, off, va, _, size, memsize, align = struct.unpack_from(
                "<IIQQQQQQ", self.data, h[5] + i * h[9])
            if typ == 1:
                self.loads.append((va, va + memsize, off, flags, size))
            if typ == 3:
                self.interpreter = self.data[off:off + size].rstrip(b"\0").decode()
            if typ == 2:
                for pos in range(off, off + size, 16):
                    tag, value = struct.unpack_from("<qQ", self.data, pos)
                    if tag == 0:
                        break
                    self.dynamic_tags.add(tag)
        sections = [struct.unpack_from("<IIQQQQIIQQ", self.data, h[6] + i * h[11])
                    for i in range(h[12])]
        if h[13] >= len(sections):
            raise ValueError("ELF section name table missing")
        s = sections[h[13]]
        names = self.data[s[4]:s[4] + s[5]]
        self.sections = {}
        self.build_id = None
        for s in sections:
            name = names[s[0]:].split(b"\0", 1)[0].decode(errors="replace")
            self.sections[name] = s
            if name == ".note.gnu.build-id":
                off, end = s[4], s[4] + s[5]
                while off + 12 <= end:
                    n, d, typ = struct.unpack_from("<III", self.data, off)
                    desc = off + 12 + ((n + 3) & ~3)
                    if typ == 3 and self.data[off + 12:off + 15] == b"GNU":
                        self.build_id = self.data[desc:desc + d].hex()
                    off = desc + ((d + 3) & ~3)

    def contains(self, va):
        return any(lo <= va < hi and flags & 1 for lo, hi, _, flags, _ in self.loads)

    def metadata(self):
        st = self.path.stat()
        return {"path": str(self.path), "sha256": sha256(self.path), "build_id": self.build_id,
                "machine": self.machine, "type": self.type, "bytes": st.st_size,
                "relocations": any(n.startswith((".rela.text", ".rel.text")) for n in self.sections),
                "symbols": ".symtab" in self.sections, "go": any("gopclntab" in n or n == ".go.buildinfo" for n in self.sections),
                "interpreter": self.interpreter,
                "entry": self.entry,
                "rpath_kind": "rpath" if 15 in self.dynamic_tags else "runpath" if 29 in self.dynamic_tags else None}


def is_executable_image(row):
    return bool(row["type"] == 2 or row["interpreter"] or row.get("entry", 0))


def payload_hash(path,table_start,table_end):
    h=hashlib.sha256()
    with path.open("rb") as f:
        remaining=table_start
        while remaining:
            block=f.read(min(1024*1024,remaining));h.update(block);remaining-=len(block)
        f.seek(table_end)
        for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
    return h.hexdigest()


def normalize_loads(source,target):
    with source.open("rb") as f:
        h=struct.unpack("<16sHHIQQQIHHHHHH",f.read(64))
        if h[0][:6]!=b"\x7fELF\x02\x01" or h[1]!=3 or h[2]!=183:
            raise ValueError("expected AArch64 little-endian shared object")
        phoff,phsize,phnum=h[5],h[9],h[10]
        if phsize<56 or phoff+phsize*phnum>source.stat().st_size:raise ValueError("invalid program headers")
        f.seek(phoff);table=f.read(phsize*phnum)
        entries=[table[i*phsize:(i+1)*phsize] for i in range(phnum)]
        values=[struct.unpack_from("<IIQQQQQQ",entry) for entry in entries]
        slots=[i for i,v in enumerate(values) if v[0]==1]
        loads=[values[i] for i in slots]
        for v in loads:
            typ,flags,offset,va,pa,fs,ms,align=v
            if fs>ms or offset+fs>source.stat().st_size:raise ValueError("invalid PT_LOAD size")
            if align>1 and (align&(align-1) or va%align!=offset%align):raise ValueError("invalid PT_LOAD alignment")
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
    return {"changed":new_table!=table,"program_header_offset":phoff,
            "before_load_vaddrs":[v[3] for v in loads],"after_load_vaddrs":[v[3] for v in ordered_values],
            "all_bytes_outside_program_headers_sha256":before,"code_and_data_payload_unchanged":True}

