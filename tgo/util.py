import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


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


def run(command, log, **kwargs):
    command = [str(x) for x in command]
    start = time.monotonic()
    with Path(log).open("w") as f:
        p = subprocess.run(command, stdout=f, stderr=subprocess.STDOUT, **kwargs)
    record = {"command": command, "returncode": p.returncode,
              "seconds": time.monotonic() - start, "log": str(log)}
    if p.returncode:
        raise RuntimeError(f"command failed ({p.returncode}): {command[0]}; see {log}\n" +
                           Path(log).read_text(errors="replace")[-2000:])
    return record


def tool(name, override=None, required=True):
    candidates = [override, os.environ.get("TGO_" + name.upper().replace("-", "_"))]
    root = Path(__file__).resolve().parents[1]
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
        for k in ("PYTHONHOME", "PYTHONPATH", "PATH"):
            saved = env.pop("TGO_SAVED_" + k, "__UNSET__")
            if saved == "__UNSET__":
                env.pop(k, None)
            else:
                env[k] = saved
    return env
