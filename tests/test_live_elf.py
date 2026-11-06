from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from tgo.live.aarch64_elf import ELF

class LiveELF(unittest.TestCase):
    def test_extended_counts_and_bad_header_bounds_use_checked_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'fixture.c';source.write_text('int value(void){return 7;}')
            binary=root/'fixture.so';subprocess.run(['cc','-shared','-fPIC',str(source),'-o',str(binary)],check=True)
            data=bytearray(binary.read_bytes());struct.pack_into('<H',data,18,183)
            binary.write_bytes(data);original=ELF(binary)
            header=struct.unpack_from('<16sHHIQQQIHHHHHH',data);zero=header[6]
            struct.pack_into('<Q',data,zero+32,header[12]);struct.pack_into('<I',data,zero+40,header[13]);struct.pack_into('<I',data,zero+44,header[10])
            struct.pack_into('<H',data,56,0xffff);struct.pack_into('<H',data,60,0);struct.pack_into('<H',data,62,0xffff)
            binary.write_bytes(data);extended=ELF(binary)
            self.assertEqual(extended.symbols,original.symbols)
            self.assertEqual(len(extended.sections),len(original.sections))
            struct.pack_into('<Q',data,40,2**64-64);binary.write_bytes(data)
            with self.assertRaisesRegex(ValueError,'outside ELF'):ELF(binary)

if __name__=='__main__':unittest.main()
