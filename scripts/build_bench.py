#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
out = root / "build/bench"
out.mkdir(parents=True, exist_ok=True)
subprocess.run([os.environ.get("CC", "cc"), "-O2", "-fPIC", "-shared", "-fno-inline",
                "-Wl,--emit-relocs", "-g", str(root / "benchmarks/native_frontend.c"),
                "-o", str(out / "libfrontend.so")], check=True)
print(out / "libfrontend.so")
