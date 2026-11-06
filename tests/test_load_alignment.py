import ctypes
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from tgo.elf import ELF,normalize_loads

class LoadHeaderNormalization(unittest.TestCase):
    def fixture(self,root):
        source=root/'f.c';source.write_text('int value=19;int f(int x){return value+x*3;}')
        target=root/'input.so';subprocess.run(['cc','-shared','-fPIC',source,'-o',target],check=True)
        return target

    def test_reduces_only_an_inconsistent_alignment_and_loads_correctly(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);original=self.fixture(root);raw=bytearray(original.read_bytes())
            h=struct.unpack_from('<16sHHIQQQIHHHHHH',raw)
            selected=None
            for i in range(h[10]):
                offset=h[5]+i*h[9];p=struct.unpack_from('<IIQQQQQQ',raw,offset)
                if p[0]==1 and p[2]!=p[3] and (p[2]-p[3])%(2<<20):selected=offset;break
            if selected is None:self.skipTest('linker emits no shifted data load')
            struct.pack_into('<Q',raw,selected+48,2<<20)
            malformed=root/'oversized-align.so';malformed.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'alignment'):ELF(malformed)
            fixed=root/'fixed.so';result=normalize_loads(malformed,fixed)
            self.assertEqual(len(result['alignment_changes']),1)
            self.assertTrue(result['code_and_data_payload_unchanged'])
            ELF(fixed)
            library=ctypes.CDLL(str(fixed));self.assertEqual(library.f(7),40)

    def test_unmappable_load_is_rejected_instead_of_relaxing_real_page_alignment(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=self.fixture(root);raw=bytearray(source.read_bytes())
            h=struct.unpack_from('<16sHHIQQQIHHHHHH',raw)
            for i in range(h[10]):
                offset=h[5]+i*h[9];p=struct.unpack_from('<IIQQQQQQ',raw,offset)
                if p[0]==1:struct.pack_into('<Q',raw,offset+16,p[3]+4);break
            source.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'target page size'):normalize_loads(source,root/'out.so')

if __name__=='__main__':unittest.main()
