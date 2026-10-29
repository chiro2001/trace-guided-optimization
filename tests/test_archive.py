from pathlib import Path
import subprocess
import tempfile
import unittest
from tgo.instrument import archive_member


class RuntimeArchive(unittest.TestCase):
    def test_real_gnu_archive_and_long_names_are_read_without_an_ar_runtime_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'f.c';source.write_text('int f(void){return 7;}')
            names=['instr.cpp.o','a-long-object-file-name-with-more-than-sixteen-bytes.o']
            for name in names:subprocess.run(['cc','-c',source,'-o',root/name],check=True)
            archive=root/'runtime.a';subprocess.run(['ar','rcs',archive,*[root/name for name in names]],check=True)
            for name in names:self.assertEqual(archive_member(archive,name),(root/name).read_bytes())
            with self.assertRaises(ValueError):archive_member(archive,'absent.o')
            archive.write_bytes(archive.read_bytes()[:-10])
            with self.assertRaises(ValueError):archive_member(archive,names[1])


if __name__=='__main__':unittest.main()
