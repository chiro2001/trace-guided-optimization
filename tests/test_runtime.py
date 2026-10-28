import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import venv
from tgo.runtime import build_runtime, make_launcher


class RuntimeTests(unittest.TestCase):
    def test_entry_carries_its_python_roi_helper(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); build_runtime(root)
            entry = root / "entry"
            make_launcher(root, entry, ["/usr/bin/python3"], [], cwd=str(root))
            output = subprocess.check_output([entry, "-c", "import tgo.roi;print(tgo.roi.__file__)"], text=True)
            self.assertEqual(Path(output.strip()), root / "support/tgo/roi.py")

    def test_virtualenv_executable_keeps_its_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prefix = root / "venv"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(prefix)
            build_runtime(root)
            entry = prefix / "bin/python.tgo"
            make_launcher(root, entry, [str(prefix / "bin/python")], [])
            output = subprocess.check_output([entry, "-c", "import sys;print(sys.prefix)"], text=True)
            self.assertEqual(output.strip(), str(prefix))

    def test_absolute_dlopen_and_collision_and_origin(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            build_runtime(root)
            def lib(folder, number):
                p = root / folder; p.mkdir()
                src = p / "same.c"; src.write_text(f"int answer(void) {{ return {number}; }}")
                out = p / "same.so"
                subprocess.run(["cc", "-shared", "-fPIC", src, "-o", out], check=True)
                return out
            a, b, other = lib("a", 11), lib("b", 42), lib("other", 7)
            entry = root / "entry"
            py = "import ctypes,sys;print(ctypes.CDLL(sys.argv[1]).answer(),ctypes.CDLL(sys.argv[2]).answer());print(sys.argv[3:])"
            make_launcher(root, entry, ["/usr/bin/python3", "-c", py, str(a), str(other)],
                          [{"original": str(a), "optimized": str(b)}])
            output = subprocess.check_output([entry, "space arg", "中文"], text=True)
            self.assertEqual(output, "42 7\n['space arg', '中文']\n")
            env = os.environ.copy(); env["TGO_DISABLE"] = "1"
            self.assertTrue(subprocess.check_output([entry], env=env, text=True).startswith("11 7"))
            a.touch()
            self.assertEqual(subprocess.run([entry], capture_output=True).returncode, 125)

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); build_runtime(root)
            p = root / "existing"; p.write_text("preserve")
            with self.assertRaises(FileExistsError):
                make_launcher(root, p, ["/usr/bin/true"], [])
            self.assertEqual(p.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
