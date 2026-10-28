"""Explicit ROI API usable without profiler. Also emits the established QEMU markers."""
import ctypes
from contextlib import contextmanager
import os
import platform
import time

_lib = None
_syscall = ctypes.CDLL(None).syscall
_syscall.restype = ctypes.c_long


def _marker(begin):
    global _lib
    if os.environ.get("TGO_ROI_LIBRARY"):
        if _lib is None:
            _lib = ctypes.CDLL(os.environ["TGO_ROI_LIBRARY"])
        getattr(_lib, "tgo_roi_begin" if begin else "tgo_roi_end")()
    else:
        nr = {"aarch64": 172, "x86_64": 39}.get(platform.machine())
        if nr is None:
            raise RuntimeError("ROI markers currently support aarch64/x86_64")
        _syscall(ctypes.c_long(nr), ctypes.c_ulonglong(
            0x51554D4153544152 if begin else 0x51554D4153544F50), ctypes.c_long(0))


@contextmanager
def region(name="roi"):
    _marker(True)
    pmu = None
    if os.environ.get("TGO_NATIVE_COUNTERS") == "1":
        from .pmu import Group
        pmu = Group()
        pmu.start()
    start = time.perf_counter_ns()
    try:
        yield
    finally:
        elapsed = time.perf_counter_ns() - start
        counters = None
        if pmu:
            try:
                counters = pmu.stop()
            finally:
                pmu.close()
        _marker(False)
        metric = os.environ.get("TGO_METRIC_FILE")
        if metric:
            import json
            with open(metric, "a") as f:
                f.write(json.dumps({"region": name, "nanoseconds": elapsed, "pid": os.getpid(), "pmu": counters}) + "\n")


def install_hook(spec):
    """Wrap module:Class.method; import hooks are installed before running the entry script."""
    import functools
    import importlib
    module, sep, dotted = spec.partition(":")
    if not sep or not dotted:
        raise ValueError("hook must be module:Class.method")
    obj = importlib.import_module(module)
    parts = dotted.split(".")
    for p in parts[:-1]:
        obj = getattr(obj, p)
    name = parts[-1]
    original = getattr(obj, name)
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        with region(spec):
            return original(*args, **kwargs)
    setattr(obj, name, wrapped)


def emit_result(value):
    """Write deterministic correctness evidence; logs may contain timings/PIDs."""
    path = os.environ.get("TGO_RESULT_FILE")
    if path:
        import json
        from pathlib import Path
        Path(path).write_text(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
