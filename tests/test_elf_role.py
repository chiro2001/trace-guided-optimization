import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
from tgo.elf import ELF,is_executable_image


class ExecutableRoles(unittest.TestCase):
    def test_nonzero_entry_shared_library_is_still_a_loadable_dso(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'lib.c';source.write_text('int f(void){return 42;}')
            lib=root/'lib.so';subprocess.run(['cc','-shared','-fPIC','-Wl,-e,f',source,'-o',lib],check=True)
            metadata=ELF(lib).metadata();self.assertGreater(metadata['entry'],0)
            self.assertFalse(is_executable_image(metadata))
            metadata.pop('pie');self.assertFalse(is_executable_image(metadata))
            self.assertEqual(ctypes.CDLL(str(lib)).f(),42)

    def test_dynamic_and_static_pie_remain_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'main.c';source.write_text('int main(void){return 0;}')
            for mode in ('-pie','-static-pie'):
                binary=root/mode.removeprefix('-')
                compiled=subprocess.run(['cc',mode,source,'-o',binary],capture_output=True)
                if compiled.returncode and mode=='-static-pie':continue
                self.assertEqual(compiled.returncode,0,compiled.stderr)
                metadata=ELF(binary).metadata();self.assertTrue(is_executable_image(metadata))
                metadata.pop('pie');self.assertTrue(is_executable_image(metadata))
                subprocess.run([binary],check=True)


if __name__=='__main__':unittest.main()
