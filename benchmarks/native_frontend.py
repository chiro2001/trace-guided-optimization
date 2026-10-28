import argparse
import ctypes
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tgo.roi import region

p = argparse.ArgumentParser()
p.add_argument("--library", default="build/bench/libfrontend.so")
p.add_argument("--iterations", type=int, default=5000000)
a = p.parse_args()
lib = ctypes.CDLL(str(Path(a.library).resolve()))
lib.frontend_work.argtypes = [ctypes.c_uint64]
lib.frontend_work.restype = ctypes.c_uint64
lib.frontend_work(1000)
with region("decode"):
    checksum = lib.frontend_work(a.iterations)
print(checksum)
