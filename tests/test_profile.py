from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
from tgo.elf import ELF
from tgo.profile import qemu_profile


class ProfileTests(unittest.TestCase):
    def test_identical_dsos_at_different_paths_keep_separate_profiles(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = root / "lib.c"; c.write_text("int f(int x) { return x+1; }")
            a, b = root / "a.so", root / "b.so"
            subprocess.run(["cc", "-shared", "-fPIC", c, "-o", a], check=True)
            shutil.copy2(a, b)
            lo = next(v[0] for v in ELF(a).loads if v[3] & 1)
            base1, base2 = 0x70000000, 0x80000000
            maps = root / "capture.1.images"
            maps.write_text(f"M\t{base1:x}\t{a}\nM\t{base2:x}\t{b}\n")
            (root / "capture.1.trace").write_text("H\t30\t0\t1\n" + ''.join(
                f"N\t{base+lo:x}\t{base+lo+4:x}\t{n}\nF\t{base+lo:x}\t{base+lo+4:x}\t{n//2}\n"
                for base, n in [(base1, 10), (base2, 20)]))
            result = qemu_profile(root)
            self.assertEqual(len(result["images"]), 2)
            self.assertEqual(len({r["preaggregate"] for r in result["images"]}), 2)
            maps.write_text(f"M\t{base1:x}\t{a}\nM\t{base1:x}\t{b}\n")
            with self.assertRaisesRegex(ValueError, "overlapping image lifetimes"):
                qemu_profile(root)

    def test_load_bias_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = root / "lib.c"; c.write_text("int f(int x) { return x+1; }")
            lib = root / "lib.so"
            subprocess.run(["cc", "-shared", "-fPIC", c, "-o", lib], check=True)
            elf = ELF(lib)
            lo = next(v[0] for v in elf.loads if v[3] & 1)
            bias = 0x70000000
            (root / "capture.1.images").write_text(f"M\t{bias:x}\t{lib}\n")
            trace = root / "capture.1.trace"
            trace.write_text(f"H\t20\t0\t1\nT\t{bias+lo:x}\t{bias+lo+4:x}\t10\nN\t{bias+lo:x}\t{bias+lo+4:x}\t20\n")
            result = qemu_profile(root)
            self.assertEqual(result["images"][0]["dynamic_instructions"], 20)
            self.assertIn(f"F {lo:x} {lo+4:x} 10", Path(result["images"][0]["preaggregate"]).read_text())
            trace.write_text("H\t20\t1\t1\n")
            with self.assertRaisesRegex(ValueError, "instruction limit"):
                qemu_profile(root)


if __name__ == "__main__":
    unittest.main()
