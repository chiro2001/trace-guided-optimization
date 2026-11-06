import os
from pathlib import Path
import struct
import shutil
import tempfile
from .util import run, tool, sha256

NATIVE = Path(__file__).parent / "native"

def publication_environment(environment,affinity='inherit'):
    if affinity not in ('inherit','workload'):raise ValueError('未知发布亲和性策略')
    result=dict(environment)
    if affinity=='inherit':result.pop('TGO_WORKLOAD_CPUS',None)
    return result


def build_runtime(out, qemu_header=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    support = out / "support/tgo"
    support.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "roi.py", "pmu.py", "python_entry.py", "training.py", "util.py"):
        shutil.copy2(NATIVE.parent / name, support / name)
    workloads=NATIVE.parent/'workloads'
    if workloads.is_dir():
        shutil.copytree(workloads,support/'workloads',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    shipped = os.environ.get("TGO_RUNTIME_DIR")
    if shipped:
        for name in ("libtgo_audit.so", "libtgo_roi.so", "libtgo_perf_spe.so", "launcher", "go-tool", "libtgo_qemu.so"):
            p = Path(shipped) / name
            if p.exists():
                shutil.copy2(p, out / name)
        return out
    cc = tool("cc")
    for name in ("audit", "roi", "perf_spe"):
        target = out / f"libtgo_{name}.so"
        source = NATIVE / f"{name}.c"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            run([cc, "-O2", "-Wall", "-Wextra", "-shared", "-fPIC", "-I", NATIVE / "include", source, "-o", target],
                out / f"{name}.build.log")
    launcher = out / "launcher"
    identity_header = NATIVE / "include/tgo_sha256.h"
    if not launcher.exists() or launcher.stat().st_mtime < max((NATIVE / "launcher.c").stat().st_mtime, identity_header.stat().st_mtime):
        run([cc, "-O2", "-Wall", "-Wextra", NATIVE / "launcher.c", "-o", launcher],
            out / "launcher.build.log")
    go_tool = out / "go-tool"
    if not go_tool.exists() or go_tool.stat().st_mtime < max((NATIVE / "go_tool.c").stat().st_mtime, identity_header.stat().st_mtime):
        run([cc, "-O2", "-Wall", "-Wextra", NATIVE / "go_tool.c", "-o", go_tool], out / "go-tool.build.log")
    if qemu_header:
        source = NATIVE / "qemu.c"
        target = out / "libtgo_qemu.so"
        # QEMU's header exposes GLib only through opaque pointer types that this
        # plugin does not use. Keep its API declarations and forward-declare these
        # types, so recording needs neither GLib headers nor a plugin GLib link.
        include = out / "include"
        include.mkdir(exist_ok=True)
        api = Path(qemu_header).read_text().replace("#include <glib.h>",
            "typedef struct _GArray GArray;\ntypedef struct _GByteArray GByteArray;\ntypedef struct _GString GString;")
        (include / "qemu-plugin.h").write_text(api)
        run([cc, "-O2", "-Wall", "-Wextra", "-shared", "-fPIC", "-pthread", "-I", include,
             source, "-o", target], out / "qemu.build.log")
    return out


def make_launcher(out, entry, command, images, env=None, cwd=None, check_paths=()):
    """Immutable native launcher. Command and mapping are compiled into it."""
    out, entry = Path(out).resolve(), Path(entry).absolute()
    if entry.exists():
        raise FileExistsError(f"entry already exists: {entry}")
    audit = out / "libtgo_audit.so"
    # Each image is in its own directory. This prevents basename collisions.
    mapfile = out / "redirect.tsv"
    for row in images:
        if any(c in row[k] for c in "\t\n" for k in ("original", "optimized")):
            raise ValueError("ELF paths containing tabs/newlines are not supported")
    mapfile.write_text(''.join(f"{Path(r['original']).resolve()}\t{Path(r['optimized']).resolve()}\n" for r in images))
    executable = next((r["optimized"] for r in images
                       if Path(r["original"]).resolve() == Path(command[0]).resolve()), command[0])
    needs_audit = any(Path(r["original"]).resolve() != Path(command[0]).resolve() for r in images)
    if not needs_audit:
        from .elf import ELF
        for row in images:
            metadata=row.get('input',{})
            interpreter=metadata['interpreter'] if 'interpreter' in metadata else ELF(row['original']).interpreter
            if interpreter:needs_audit=True;break
    checks = list(dict.fromkeys([p for r in images for p in (r["original"], r["optimized"])] + list(map(str, check_paths))))
    command = list(command)
    if len(command) > 3 and Path(command[1]).name == "python_entry.py" and command[2] == "--hook":
        # A published entry must not depend on the capture directory's hook copy.
        for name in ("__init__.py", "roi.py", "pmu.py", "python_entry.py"):
            shutil.copy2(Path(command[1]).parent / name, out / "support/tgo" / name)
        command[1] = str(out / "support/tgo/python_entry.py")
    env = dict(env or {})
    env["TGO_PYTHON_SUPPORT"] = str(out / "support")
    if (out / "libtgo_roi.so").exists():
        env.setdefault("TGO_ROI_LIBRARY", str(out / "libtgo_roi.so"))
    def string(s):
        b = os.fsencode(s)
        return struct.pack("<I", len(b)) + b
    payload = b''.join(string(s) for s in (executable, str(audit) if needs_audit else "", str(mapfile), cwd or ""))
    payload += struct.pack("<I", len(command)) + b''.join(string(s) for s in command)
    payload += struct.pack("<I", len(env)) + b''.join(string(k) + string(v) for k, v in env.items())
    payload += struct.pack("<I", len(checks))
    expected = {}
    for row in images:
        for path_key, metadata_key in (("original", "input"), ("optimized", "output")):
            if row.get(metadata_key, {}).get("sha256"):
                expected[row[path_key]] = row[metadata_key]["sha256"]
    for path in checks:
        st = Path(path).stat()
        digest = sha256(path)
        if path in expected and digest != expected[path]:
            raise ValueError(f"ELF changed before entry publication: {path}")
        payload += string(str(path)) + struct.pack("<qqqQQqq", st.st_size,
            st.st_mtime_ns // 10**9, st.st_mtime_ns % 10**9, st.st_ino, st.st_dev,
            st.st_ctime_ns // 10**9,st.st_ctime_ns % 10**9)
        payload += bytes.fromhex(digest)
    template = (out / "launcher").read_bytes()
    if b"TGO_LAUNCHER_ABI_3\0" not in template:
        raise ValueError("runtime launcher ABI mismatch; rebuild TGO and unset an older TGO_RUNTIME_DIR")
    if env.get('TGO_WORKLOAD_CPUS') and b'TGO_LAUNCHER_CPU_AFFINITY_1\0' not in template:
        raise ValueError('预编译入口缺少亲和性能力，请重建runtime或取消旧TGO_RUNTIME_DIR')
    with tempfile.NamedTemporaryFile(dir=entry.parent, prefix=".tgo-entry-", delete=False) as f:
        tmp = Path(f.name)
        f.write(template)
        f.write(payload)
        f.write(struct.pack("<Q8s", len(payload), b"TGOCFG03"))
    tmp.chmod(0o755)
    # Atomic publication refuses overwrite, including races.
    try:
        os.link(tmp, entry)
    finally:
        tmp.unlink()
    return str(entry)
