import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tgo.runtime import build_runtime,make_launcher,publication_environment
from tgo.util import cpu_set

class WorkloadAffinity(unittest.TestCase):
    def test_publication_defaults_to_callers_affinity_instead_of_captured_single_cpu(self):
        allowed=sorted(os.sched_getaffinity(0));captured={'TGO_WORKLOAD_CPUS':str(allowed[0])}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);build_runtime(root)
            for mode in ['inherit','workload']:
                entry=make_launcher(root,root/mode,[sys.executable,'-c','import json,os;print(json.dumps(sorted(os.sched_getaffinity(0))))'],[],
                    publication_environment(captured,mode))
                output=json.loads(subprocess.check_output([entry],text=True))
                self.assertEqual(output,allowed if mode=='inherit' else allowed[:1])
    def test_published_native_entry_applies_saved_cpu_set(self):
        allowed=sorted(os.sched_getaffinity(0))[:2]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);build_runtime(root)
            entry=make_launcher(root,root/'run',[sys.executable,'-c','import json,os;print(json.dumps(sorted(os.sched_getaffinity(0))))'],[],
                {'TGO_WORKLOAD_CPUS':','.join(map(str,allowed))})
            result=json.loads(subprocess.check_output([entry],text=True))
            self.assertEqual(result,allowed)
            self.assertEqual(sorted(os.sched_getaffinity(0))[:2],allowed)

    def test_cpu_ranges_are_explicit_and_invalid_values_fail(self):
        self.assertEqual(cpu_set('0,2-4,3'),'0,2,3,4')
        for value in ['','2-1','1,','-2','1-2-3','1048576','0;echo bad']:
            with self.subTest(value=value),self.assertRaises(ValueError):cpu_set(value)

if __name__=='__main__':unittest.main()
