import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tgo.capabilities import inspect_image
from tgo.elf import ELF

class LargeELFMetadata(unittest.TestCase):
    def test_diagnostic_reads_only_referenced_metadata_and_never_hashes_application(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'f.c';source.write_text('int f(void){return 9;}')
            image=root/'image.so';subprocess.run(['cc','-shared','-fPIC',source,'-o',image],check=True)
            with image.open('ab') as stream:stream.truncate(256*1024*1024)
            original=os.pread;requests=[]
            def read(fd,size,offset):requests.append(size);return original(fd,size,offset)
            with patch('tgo.elf.os.pread',side_effect=read),patch('tgo.elf.sha256',side_effect=AssertionError('资格诊断不可哈希应用')):
                result=inspect_image(image)
            self.assertFalse(result['known_input_allowlist']);self.assertGreater(result['function_symbols'],0)
            self.assertLess(sum(requests),1024*1024)

    def test_lazy_payload_rejects_file_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'f.c';source.write_text('int f(void){return 9;}')
            image=root/'image.so';subprocess.run(['cc','-shared','-fPIC',source,'-o',image],check=True)
            elf=ELF(image);copy=root/'replacement';copy.write_bytes(image.read_bytes());copy.replace(image)
            with self.assertRaisesRegex(ValueError,'changed'):_=elf.data

    def test_nonregular_input_is_rejected_without_blocking_on_fifo(self):
        with tempfile.TemporaryDirectory() as folder:
            image=Path(folder)/'fifo';os.mkfifo(image)
            with self.assertRaisesRegex(ValueError,'regular file'):ELF(image)

if __name__=='__main__':unittest.main()
