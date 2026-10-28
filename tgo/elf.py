"""Small, dependency-free ELF64 reader. All profile addresses are ELF virtual addresses."""
from pathlib import Path
import struct
from .util import sha256


class ELF:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        if self.data[:6] != b"\x7fELF\x02\x01":
            raise ValueError(f"requires little-endian ELF64: {path}")
        h = struct.unpack_from("<16sHHIQQQIHHHHHH", self.data)
        self.type, self.machine = h[1:3]
        self.loads = []
        self.interpreter = None
        for i in range(h[10]):
            typ, flags, off, va, _, size, memsize, align = struct.unpack_from(
                "<IIQQQQQQ", self.data, h[5] + i * h[9])
            if typ == 1:
                self.loads.append((va, va + memsize, off, flags, size))
            if typ == 3:
                self.interpreter = self.data[off:off + size].rstrip(b"\0").decode()
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
                "symbols": ".symtab" in self.sections, "go": ".gopclntab" in self.sections,
                "interpreter": self.interpreter}
