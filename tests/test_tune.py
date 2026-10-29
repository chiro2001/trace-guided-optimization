import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tgo.elf import ELF
from tgo.tune import tune, compose_bundle
from tgo.util import write_json, read_json, sha256


class SubsetSelection(unittest.TestCase):
    def test_bad_rewritten_library_is_isolated_and_safe_subset_is_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); capture=root/"capture";capture.mkdir()
            objects=root/"objects";objects.mkdir()
            images=[]
            for name, original_value, candidate_value in (("a",2,2),("b",3,999)):
                original=root/(name+".so"); candidate=objects/(name+"-optimized.so")
                source=root/(name+".c")
                for value,target in ((original_value,original),(candidate_value,candidate)):
                    source.write_text(f"int f(void){{return {value};}}\n")
                    subprocess.run(["cc","-shared","-fPIC",source,"-o",target],check=True)
                images.append({"original":str(original),"optimized":str(candidate),
                               "input":ELF(original).metadata(),"output":ELF(candidate).metadata()})
            driver=root/"workload.py"
            # Deterministic fixture metrics exercise selection, not hardware
            # performance. Actual ELF redirection and output checks are real.
            driver.write_text("import ctypes,json,os\nfrom pathlib import Path\n"
                f"print(sum(ctypes.CDLL(p).f() for p in {[r['original'] for r in images]!r}))\n"
                "if os.environ.get('TGO_METRIC_FILE'):\n"
                " fast='a-optimized.so' in Path('/proc/self/maps').read_text()\n"
                " Path(os.environ['TGO_METRIC_FILE']).write_text(json.dumps({'region':'fixture','nanoseconds':50000000 if fast else 100000000})+'\\n')\n")
            write_json(capture/"run.json",{"state":"recorded","backend":"qemu","command":[sys.executable,str(driver)],"cwd":str(root),"env":{}})
            write_json(capture/"profile.json",{"backend":"qemu","images":[r["input"] for r in images]})
            template={"source_run":str(capture),"profile_sha256":sha256(capture/"profile.json"),"workload":{}}
            def build(args):
                built=compose_bundle(Path(args.output),template,images)
                built["failed_images"]=[]
                write_json(Path(args.output)/"build.json",built)
                return built
            args=argparse.Namespace(output=str(root/"tuned"),run=str(capture),strategy="bolt",layout=["ext-tsp"],
                image_search="adaptive",image=[],bolt_mode="auto",bolt_arg=[],entry=None,entry_mode="command",
                timeout=10,metric="fixture",search_pairs=2,validate=2,minimum_gain=0.5,confirm_candidates=2)
            with patch("tgo.tune.optimize",side_effect=build) as rewrite:
                result=tune(args)
                self.assertEqual(rewrite.call_count,1)
            self.assertNotEqual(result["selected"],"baseline")
            built=read_json(root/"tuned/build.json")
            self.assertEqual([r["original"] for r in built["images"]],[images[0]["original"]])
            self.assertTrue(result["confirmation"]["equivalent"])
            self.assertEqual(subprocess.check_output([result["entry"]],text=True),"5\n")
            self.assertFalse((root/"tuned/ext-tsp/run").exists())
            self.assertTrue(any(not p["validation"]["equivalent"] for p in result["probes"]))


if __name__ == "__main__":
    unittest.main()
