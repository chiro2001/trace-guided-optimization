from pathlib import Path
import sys
import tempfile
import time
import unittest
from tgo.util import write_json
from tgo.validate import validate
from tgo.runtime import build_runtime, make_launcher


class ValidationTimeout(unittest.TestCase):
    def test_standalone_roi_workload_gets_helper_for_both_validation_arms(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);build_runtime(root)
            capture=root/"capture";capture.mkdir()
            driver=root/"standalone.py"
            driver.write_text("import os\nfrom pathlib import Path\nfrom tgo.roi import region\n"
                "assert Path(os.environ['TGO_ROI_LIBRARY']).is_file()\n"
                "with region('fixture'):print('ok')\n")
            command=[sys.executable,str(driver)]
            write_json(capture/"run.json",{"command":command,"env":{"PYTHONPATH":""},"cwd":str(root)})
            entry=make_launcher(root,root/"run",command,[],cwd=str(root))
            write_json(root/"build.json",{"source_run":str(capture),"entry":entry,"images":[]})
            result=validate(root,pairs=1,metric="fixture")
            self.assertTrue(result["equivalent"])
            self.assertFalse(result["pairs"][0]["baseline"]["metric_missing"])

    def test_repeated_validation_preserves_each_rounds_raw_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "capture"; source.mkdir()
            write_json(source / "run.json", {"command": [sys.executable, "-c", "print('ok')"],
                                             "env": {}, "cwd": d})
            write_json(root / "build.json", {"source_run": str(source), "entry": sys.executable,
                                             "entry_mode": "executable", "images": []})
            first = validate(root, pairs=1)
            prefix = first["pairs"][0]["candidate"]["evidence_prefix"]
            raw = Path(prefix + ".stdout").read_bytes()
            second = validate(root, pairs=1)
            self.assertTrue(first["equivalent"] and second["equivalent"])
            self.assertNotEqual(first["evidence_directory"], second["evidence_directory"])
            self.assertEqual(Path(prefix + ".stdout").read_bytes(), raw)
            self.assertTrue((Path(first["evidence_directory"]) / "validation.json").exists())

    def test_hanging_candidate_stops_its_process_group_and_rejects(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "capture"; source.mkdir()
            write_json(source / "run.json", {"command": [sys.executable, "-c", "print('ok')"],
                                             "env": {}, "cwd": d})
            entry = root / "bad-entry"
            entry.write_text(f"#!{sys.executable}\nimport subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\ntime.sleep(60)\n")
            entry.chmod(0o755)
            write_json(root / "build.json", {"source_run": str(source), "entry": str(entry), "images": []})
            start = time.monotonic()
            result = validate(root, pairs=3, timeout=0.4)
            self.assertLess(time.monotonic() - start, 3)
            self.assertFalse(result["equivalent"])
            self.assertTrue(result["pairs"][0]["candidate"]["timed_out"])
            self.assertEqual(result["completed_pairs"], 1)


if __name__ == "__main__":
    unittest.main()
