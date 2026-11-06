from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tgo.elf import ELF
from tgo.scenarios import validate_scenarios,read_scenarios
from tgo.selection import screening_score,confirmed
from tgo.tune import compose_bundle
from tgo.util import write_json

class MultipleScenarios(unittest.TestCase):
    def test_average_gain_cannot_hide_regressing_scenario(self):
        validation={'equivalent':True,'requested_pairs':4,'completed_pairs':4,
            'median_reduction_percent':35.,'worst_case_median_reduction_percent':-20.,'min_reduction_percent':-20.}
        self.assertIsNone(screening_score(validation,.5));self.assertFalse(confirmed(validation,.5))

    def test_real_heldout_semantic_error_rejects_whole_scenario_set(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);capture=root/'capture';capture.mkdir();source=root/'f.c'
            original=root/'original.so';candidate=root/'candidate.so'
            for output,body in [(original,'return x*x+3;'),(candidate,'if(x==9)return 999;return x*x+3;')]:
                source.write_text('int f(int x){'+body+'}');subprocess.run(['cc','-shared','-fPIC',source,'-o',output],check=True)
            driver=root/'driver.py';driver.write_text('import ctypes,sys\nprint(ctypes.CDLL(sys.argv[1]).f(int(sys.argv[2])))\n')
            command=[sys.executable,str(driver),str(original),'5']
            write_json(capture/'run.json',{'command':command,'cwd':str(root),'env':{},'state':'recorded'})
            image={'original':str(original),'optimized':str(candidate),'input':ELF(original).metadata(),'output':ELF(candidate).metadata()}
            compose_bundle(root/'bundle',{'source_run':str(capture)},[image])
            cases={'schema':'tgo.validation-cases.v1','cases':[{'name':'normal','command':command},
                {'name':'unseen','command':command[:-1]+['9']}]}
            result=validate_scenarios(root/'bundle',cases,pairs=1,timeout=10)
            self.assertFalse(result['equivalent']);self.assertEqual(result['completed_pairs'],2)
            self.assertTrue(result['cases'][0]['validation']['equivalent'])
            self.assertFalse(result['cases'][1]['validation']['equivalent'])
            self.assertIsNone(screening_score(result,.5));self.assertTrue((root/'bundle/run').exists())

    def test_duplicate_or_path_like_case_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cases.json'
            for cases in [[{'name':'a'},{'name':'a'}],[{'name':'../a'}],[{'name':'a','command':'implicit shell'}]]:
                write_json(path,{'schema':'tgo.validation-cases.v1','cases':cases})
                with self.assertRaises(ValueError):read_scenarios(path)

    def test_scenario_relative_executable_and_path_resolve_in_its_own_workdir(self):
        from tgo.util import command_path
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);binary=root/'program';binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
            self.assertEqual(command_path(['./program'],cwd=root),[str(binary)])
            self.assertEqual(command_path(['program'],cwd=root,environment={'PATH':'.'}),[str(binary)])

if __name__=='__main__':unittest.main()
