from pathlib import Path
import sys
import tempfile
import time
import unittest
from tgo.util import write_json
from tgo.validate import validate


class ValidationTimeout(unittest.TestCase):
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
