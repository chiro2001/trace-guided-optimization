import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import signal


class IdentityError(ValueError):
    pass

def cpu_set(value):
    if value is None:return None
    cpus=set()
    for group in str(value).split(','):
        fields=group.split('-')
        if len(fields) not in (1,2) or not all(x.isdigit() for x in fields):raise ValueError('CPU列表需要整数、范围和逗号')
        lo,hi=int(fields[0]),int(fields[-1])
        if lo>hi or hi>=1048576:raise ValueError('CPU范围无效')
        cpus.update(range(lo,hi+1))
    if not cpus:raise ValueError('CPU列表不能为空')
    return ','.join(map(str,sorted(cpus)))

def affinity_command(command, cpus):
    normalized=cpu_set(cpus)
    return [tool('taskset'),'-c',normalized,*command] if normalized else list(command)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",encoding='utf-8')
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


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


def tool_env_key(name):
    return 'TGO_CXX' if name=='g++' else 'TGO_'+name.upper().replace('-','_')

def dependency_asset(relative,*,environment=None,project=None):
    """统一解析SDK非可执行资源，允许新安装前缀，不检查应用指纹。"""
    environment=os.environ if environment is None else environment
    root=Path(project or Path(__file__).resolve().parents[1]);prefix=environment.get('TGO_INSTALL_PREFIX')
    candidates=([Path(prefix)/relative] if prefix else [])+[root/'deps/install'/relative]
    if environment.get('TGO_APPDIR'):candidates.append(Path(environment['TGO_APPDIR'])/'usr'/relative)
    if relative=='include/qemu-plugin.h':candidates += [Path('/usr/local')/relative,Path('/usr')/relative]
    return next((str(p.resolve()) for p in candidates if p.is_file()),None)

def tool(name, override=None, required=True, *, environment=None, project=None):
    environment=os.environ if environment is None else environment
    key=tool_env_key(name)
    preferred=override if override is not None else environment.get(key) or (environment.get('TGO_G++') if name=='g++' else None)
    if preferred is not None:
        selected=shutil.which(str(preferred),path=environment.get('PATH'))
        if selected and Path(selected).is_file() and os.access(selected,os.X_OK):return str(Path(selected).absolute())
        if required:raise RuntimeError('指定工具不可执行:'+name+'='+str(preferred))
        return None
    candidates=[]
    root = Path(project or Path(__file__).resolve().parents[1])
    if environment.get("TGO_TOOL_ROOT"):
        private = "llvm-bolt" if name == "perf2bolt" else name
        candidates += [Path(environment["TGO_TOOL_ROOT"]) / private / "bin" / name]
    if name == "llvm-bolt-recover":
        candidates += [root / "deps/install/recover/bin/llvm-bolt"]
    if environment.get('TGO_INSTALL_PREFIX'):
        prefix=Path(environment['TGO_INSTALL_PREFIX'])
        if name=='llvm-bolt-recover':candidates += [prefix/'recover/bin/llvm-bolt']
        candidates += [prefix/'bin'/name]
    candidates += [root / "deps/install/bin" / name,root/'deps/bin'/name,shutil.which(name,path=environment.get('PATH'))]
    for p in candidates:
        if p and Path(p).is_file() and os.access(p, os.X_OK):
            return str(Path(p).absolute())
    if required:
        raise RuntimeError(f"missing tool: {name}; run scripts/bootstrap.py or set {key}")
    return None


def command_path(command,*,cwd=None,environment=None):
    if not command:
        raise ValueError("provide a workload after --")
    environment=workload_env() if environment is None else environment
    directory=Path(cwd or os.getcwd()).resolve();program=command[0]
    if not Path(program).is_absolute() and os.sep in program:program=str(directory/program)
    search=environment.get('PATH',os.defpath)
    search=os.pathsep.join(str(directory/part) if not Path(part or '.').is_absolute() else part for part in search.split(os.pathsep))
    binary = shutil.which(program,path=search)
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
    env['LC_ALL']='C'
    for k in ("LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH", "PYTHONPLATLIBDIR"):
        env.pop(k, None)
    if env.get("TGO_PACKAGED"):
        env.pop("LD_LIBRARY_PATH", None)
    return env
