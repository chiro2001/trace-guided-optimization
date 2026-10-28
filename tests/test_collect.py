from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tgo.collect import doctor
from tgo.util import read_json


class HardwareROI(unittest.TestCase):
    def test_marker_capture_contains_real_samples(self):
        d = doctor()
        if not d["perf_probe"] or not d["perf_probe"]["usable"]:
            self.skipTest("PMU sampling permission unavailable")
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "record"
            r = subprocess.run([sys.executable, "-m", "tgo", "record", "-o", str(output),
                                "--backend", "perf", "--roi", "marker", "--", sys.executable,
                                str(project / "benchmarks/python_frontend.py"), "--iterations", "1000000"],
                               cwd=project, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            profile = read_json(output / "profile.json")
            self.assertGreater(sum(row["sample_percent"] for row in profile["images"]), 0)
            markers = list(output.glob("capture.*.roi"))
            self.assertTrue(markers)
            text = markers[0].read_text()
            self.assertTrue(text.startswith("B\t"))
            self.assertIn("\nE\t", text)


if __name__ == "__main__":
    unittest.main()
