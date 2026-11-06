from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tgo.elf import ELF
from tgo.evaluate import heldout
from tgo.tune import compose_bundle
from tgo.util import write_json

class HeldoutValidation(unittest.TestCase):
    def test_unseen_input_detects_semantic_failure_and_withdraws_new_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);capture=root/'capture';capture.mkdir()
            source=root/'f.c';original=root/'original.so';optimized=root/'optimized.so'
            for target,body in [(original,'return x*x+3;'),(optimized,'if(x==9)return 999;return x*x+3;')]:
                source.write_text('int f(int x){'+body+'}')
                subprocess.run(['cc','-shared','-fPIC',source,'-o',target],check=True)
            driver=root/'driver.py';driver.write_text('import ctypes,sys\nprint(ctypes.CDLL(sys.argv[1]).f(int(sys.argv[2])))\n')
            command=[sys.executable,str(driver),str(original),'5']
            write_json(capture/'run.json',{'command':command,'cwd':str(root),'env':{},'state':'recorded'})
            image={'original':str(original),'optimized':str(optimized),'input':ELF(original).metadata(),'output':ELF(optimized).metadata()}
            compose_bundle(root/'trained',{'source_run':str(capture),'profile_sha256':None,'workload':{}},[image])
            result=heldout(root/'trained',root/'heldout',command[:-1]+['9'],pairs=1,timeout=10)
            self.assertFalse(result['equivalent'])
            self.assertFalse((root/'heldout/bundle/run').exists())
            self.assertTrue((root/'trained/run').is_file())
            self.assertEqual(result['optimized_images'],[str(optimized)])

if __name__=='__main__':unittest.main()
