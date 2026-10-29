import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import signal


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def terminate_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The parent may exit on TERM while a descendant ignores it. Always clean
    # the remaining group, even when wait() has already reaped the parent.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(command, log, **kwargs):
    command = [str(x) for x in command]
    start = time.monotonic()
    kwargs.setdefault("env", tool_env())
    timeout = kwargs.pop("timeout", None)
    with Path(log).open("w") as f:
        p = subprocess.Popen(command, stdout=f, stderr=subprocess.STDOUT, start_new_session=True, **kwargs)
        try:
            p.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            terminate_process_group(p)
            raise
    record = {"command": command, "returncode": p.returncode,
              "seconds": time.monotonic() - start, "log": str(log)}
    if p.returncode:
        raise RuntimeError(f"command failed ({p.returncode}): {command[0]}; see {log}\n" +
                           Path(log).read_text(errors="replace")[-2000:])
    return record


def tool(name, override=None, required=True):
    candidates = [override, os.environ.get("TGO_" + name.upper().replace("-", "_"))]
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("TGO_TOOL_ROOT"):
        private = "llvm-bolt" if name == "perf2bolt" else name
        candidates += [Path(os.environ["TGO_TOOL_ROOT"]) / private / "bin" / name]
    if name == "llvm-bolt-recover":
        candidates += [root / "deps/install/recover/bin/llvm-bolt"]
    candidates += [root / "deps/install/bin" / name, shutil.which(name)]
    for p in candidates:
        if p and Path(p).is_file() and os.access(p, os.X_OK):
            return str(Path(p).absolute())
    if required:
        raise RuntimeError(f"missing tool: {name}; run scripts/bootstrap.py or set TGO_{name.upper().replace('-', '_')}")
    return None


def command_path(command):
    if not command:
        raise ValueError("provide a workload after --")
    binary = shutil.which(command[0], path=workload_env().get("PATH"))
    if not binary:
        raise ValueError(f"executable not found: {command[0]}")
    # Preserve the symlink spelling: Python virtualenv and multicall argv[0] depend on it.
    return [str(Path(binary).absolute()), *command[1:]]


def workload_env():
    env = os.environ.copy()
    if env.get("TGO_PACKAGED"):
        for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONPLATLIBDIR", "PATH"):
            saved = env.pop("TGO_SAVED_" + k, "__UNSET__")
            if saved == "__UNSET__":
                env.pop(k, None)
            else:
                env[k] = saved
    return env


def tool_env():
    env = os.environ.copy()
    for k in ("LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH", "PYTHONPLATLIBDIR"):
        env.pop(k, None)
    if env.get("TGO_PACKAGED"):
        env.pop("LD_LIBRARY_PATH", None)
    return env
