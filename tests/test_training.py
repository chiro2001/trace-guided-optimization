import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from tgo.util import sha256


@unittest.skipUnless(shutil.which('gcc') and shutil.which('gcov-dump'),'GCC profile tools required')
class CompilerROI(unittest.TestCase):
    def test_gcc_profile_excludes_calls_before_and_after_roi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'count.c';source.write_text('int f(int x){return x>2?x+1:x-1;}')
            lib=root/'libcount.so'
            subprocess.run(['gcc','-O1','-shared','-fPIC','-fprofile-generate',source,'-o',lib,
                '-Wl,-u,__gcov_reset','-Wl,-u,__gcov_dump','-Wl,--export-dynamic-symbol=__gcov_reset',
                '-Wl,--export-dynamic-symbol=__gcov_dump'],check=True)
            hooks=root/'hooks.json';hooks.write_text(json.dumps({'kind':'gcc','library':str(lib),'sha256':sha256(lib),'log':str(root/'roi.jsonl')}))
            env=os.environ.copy();env['TGO_TRAINING_HOOKS']=str(hooks)
            env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
            program='import ctypes\nfrom tgo.roi import region\nlib=ctypes.CDLL('+repr(str(lib))+')\nfor _ in range(100):lib.f(5)\nwith region():\n for _ in range(7):lib.f(5)\nfor _ in range(100):lib.f(5)\n'
            subprocess.run([sys.executable,'-c',program],env=env,cwd=root,check=True,capture_output=True)
            self.assertEqual(len((root/'roi.jsonl').read_text().splitlines()),1)
            profile=next(root.glob('*.gcda'))
            dump=subprocess.check_output(['gcov-dump','-l',profile],text=True)
            self.assertIn('sum_max=7',dump)


if __name__=='__main__':unittest.main()
