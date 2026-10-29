from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tgo.artifact import stage_image
from tgo.elf import ELF
from tgo.util import IdentityError,sha256,tool


@unittest.skipUnless(tool('patchelf',required=False),'patchelf required')
class CandidateImport(unittest.TestCase):
    def test_origin_dependency_is_preserved_and_source_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); original=root/'original';original.mkdir();built=root/'prebuilt';built.mkdir()
            dep=original/'dep.c';dep.write_text('int dep(void){return 41;}')
            subprocess.run(['cc','-shared','-fPIC',dep,'-o',original/'libdep.so'],check=True)
            source=root/'lib.c';source.write_text('extern int dep(void);int answer(void){return dep()+1;}')
            old=original/'libexample.so'
            subprocess.run(['cc','-shared','-fPIC',source,'-L'+str(original),'-ldep','-Wl,-rpath,$ORIGIN','-o',old],check=True)
            candidate=built/'libexample.so';shutil.copy2(old,candidate);before=sha256(candidate)
            image=stage_image(old,candidate,root/'stage',before)
            self.assertTrue(image['executable_sections_unchanged'])
            self.assertEqual(sha256(candidate),before)
            code='import ctypes,sys;print(ctypes.CDLL(sys.argv[1]).answer())'
            output=subprocess.check_output([shutil.which('python3'),'-c',code,image['optimized']],cwd='/tmp',text=True)
            self.assertEqual(output.strip(),'42')
            with self.assertRaises(IdentityError):stage_image(old,candidate,root/'bad','0'*64)
            self.assertFalse((root/'bad').exists())


if __name__=='__main__':unittest.main()
