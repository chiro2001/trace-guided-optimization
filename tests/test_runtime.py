import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import venv
import shutil
from tgo.runtime import build_runtime, make_launcher
from tgo.util import sha256


class RuntimeTests(unittest.TestCase):
    def test_entry_rejects_stale_image_identity_before_publication(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);build_runtime(root)
            original=root/"original";optimized=root/"optimized"
            original.write_bytes(b"original");optimized.write_bytes(b"optimized")
            image={"original":str(original),"optimized":str(optimized),
                   "input":{"sha256":sha256(original)},"output":{"sha256":sha256(optimized)}}
            original.write_bytes(b"new original")
            with self.assertRaisesRegex(ValueError,"before entry publication"):
                make_launcher(root,root/"entry",["/usr/bin/true"],[image])
            self.assertFalse((root/"entry").exists())

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
            replacement=a.with_suffix(".replacement")
            shutil.copy2(a,replacement);os.replace(replacement,a)
            self.assertTrue(subprocess.check_output([entry],text=True).startswith("42 7"))
            with a.open("ab") as stream:stream.write(b"changed")
            self.assertEqual(subprocess.run([entry], capture_output=True).returncode, 125)

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); build_runtime(root)
            p = root / "existing"; p.write_text("preserve")
            with self.assertRaises(FileExistsError):
                make_launcher(root, p, ["/usr/bin/true"], [])
            self.assertEqual(p.read_text(), "preserve")

    def test_same_size_content_change_with_restored_mtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);build_runtime(root);artifact=root/'artifact'
            artifact.write_bytes(b'original-artifact');before=artifact.stat()
            entry=root/'entry';make_launcher(root,entry,['/usr/bin/true'],[],check_paths=[artifact])
            self.assertEqual(subprocess.run([entry],capture_output=True).returncode,0)
            artifact.write_bytes(b'modified-artifact')
            self.assertEqual(artifact.stat().st_size,before.st_size)
            os.utime(artifact,ns=(before.st_atime_ns,before.st_mtime_ns))
            result=subprocess.run([entry],capture_output=True)
            self.assertEqual(result.returncode,125);self.assertIn(b'ELF changed',result.stderr)


if __name__ == "__main__":
    unittest.main()
