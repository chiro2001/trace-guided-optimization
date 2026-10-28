import json
import os
from pathlib import Path
import shlex
import subprocess
import struct
import shutil
from .util import run, tool

NATIVE = Path(__file__).parent / "native"


def build_runtime(out, qemu_header=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    shipped = os.environ.get("TGO_RUNTIME_DIR")
    if shipped:
        for name in ("libtgo_audit.so", "libtgo_roi.so", "launcher", "libtgo_qemu.so"):
            p = Path(shipped) / name
            if p.exists():
                shutil.copy2(p, out / name)
        return out
    cc = tool("cc")
    for name in ("audit", "roi"):
        target = out / f"libtgo_{name}.so"
        source = NATIVE / f"{name}.c"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            run([cc, "-O2", "-Wall", "-Wextra", "-shared", "-fPIC", source, "-o", target],
                out / f"{name}.build.log")
    launcher = out / "launcher"
    if not launcher.exists() or launcher.stat().st_mtime < (NATIVE / "launcher.c").stat().st_mtime:
        run([cc, "-O2", "-Wall", "-Wextra", NATIVE / "launcher.c", "-o", launcher],
            out / "launcher.build.log")
    if qemu_header:
        source = NATIVE / "qemu.c"
        target = out / "libtgo_qemu.so"
        flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "glib-2.0"], text=True))
        run([cc, "-O2", "-Wall", "-Wextra", "-shared", "-fPIC", "-I", Path(qemu_header).parent,
             source, "-o", target, *flags], out / "qemu.build.log")
    return out


def cstr(s):
    # JSON's ASCII escaping is not C's universal-character grammar; emit UTF-8 octets.
    return '"' + ''.join(chr(c) if 32 <= c < 127 and c not in (34, 92)
                         else f"\\{c:03o}" for c in os.fsencode(s)) + '"'


def stamp(path):
    st = Path(path).stat()
    return f"{{{cstr(str(path))}, {st.st_size}LL, {st.st_mtime_ns // 10**9}LL, {st.st_mtime_ns % 10**9}L, {st.st_ino}ULL, {st.st_dev}ULL}}"


def make_launcher(out, entry, command, images, env=None, cwd=None):
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
    mapfile.write_text(''.join(f"{r['original']}\t{r['optimized']}\n" for r in images))
    executable = next((r["optimized"] for r in images
                       if Path(r["original"]).resolve() == Path(command[0]).resolve()), command[0])
    checks = [p for r in images for p in (r["original"], r["optimized"])]
    env = env or {}
    def string(s):
        b = os.fsencode(s)
        return struct.pack("<I", len(b)) + b
    payload = b''.join(string(s) for s in (executable, str(audit), str(mapfile), cwd or ""))
    payload += struct.pack("<I", len(command)) + b''.join(string(s) for s in command)
    payload += struct.pack("<I", len(env)) + b''.join(string(k) + string(v) for k, v in env.items())
    payload += struct.pack("<I", len(checks))
    for path in checks:
        st = Path(path).stat()
        payload += string(str(path)) + struct.pack("<qqqQQ", st.st_size,
            st.st_mtime_ns // 10**9, st.st_mtime_ns % 10**9, st.st_ino, st.st_dev)
    tmp = out / "entry.tmp"
    with tmp.open("wb") as f:
        f.write((out / "launcher").read_bytes())
        f.write(payload)
        f.write(struct.pack("<Q8s", len(payload), b"TGOCFG01"))
    tmp.chmod(0o755)
    # Atomic publication refuses overwrite, including races.
    os.link(tmp, entry)
    tmp.unlink()
    return str(entry)
