from pathlib import Path
import subprocess
import tempfile
import unittest
from tgo.elf import ELF
from tgo.profile import qemu_profile


class ProfileTests(unittest.TestCase):
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
