from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from tgo.collect import doctor,record
from tgo.util import read_json


class HardwareROI(unittest.TestCase):
    def test_adapter_recording_uses_original_working_directory(self):
        d=doctor()
        if not d['perf_probe'] or not d['perf_probe']['usable']:
            self.skipTest('PMU sampling permission unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir()
            code='from pathlib import Path\nfrom tgo.roi import region\nPath("proof").write_text(str(Path.cwd()))\nwith region():\n sum(i*i for i in range(1000000))\n'
            args=SimpleNamespace(output=str(root/'capture'),command=[sys.executable,'-c',code],
                pid=None,backend='perf',event=None,roi='marker',python_hook=None,env=[],
                branch_stack=False,perf=None,period=None,frequency=999,timeout=30,
                qemu_header=None,allow_truncated=False)
            record(args,cwd=work)
            self.assertEqual((work/'proof').read_text(),str(work))
            self.assertEqual(read_json(root/'capture/run.json')['cwd'],str(work))

    def test_marker_capture_contains_real_samples(self):
        d = doctor()
        if not d["perf_probe"] or not d["perf_probe"]["usable"]:
            self.skipTest("PMU sampling permission unavailable")
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            for backend in ("perf", "auto"):
                with self.subTest(backend=backend):
                    output = Path(tmp) / backend
                    r = subprocess.run([sys.executable, "-m", "tgo", "record", "-o", str(output),
                                        "--backend", backend, "--roi", "marker", "--", sys.executable,
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
