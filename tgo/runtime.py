import json
import os
from pathlib import Path
import shlex
import subprocess
from .util import run, tool

NATIVE = Path(__file__).parent / "native"


def build_runtime(out, qemu_header=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cc = tool("cc")
    for name in ("audit", "roi"):
        target = out / f"libtgo_{name}.so"
        source = NATIVE / f"{name}.c"
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            run([cc, "-O2", "-Wall", "-Wextra", "-shared", "-fPIC", source, "-o", target],
                out / f"{name}.build.log")
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
    header = out / "launcher_config.h"
    checks = [p for r in images for p in (r["original"], r["optimized"])]
    fixed = command
    env = env or {}
    header.write_text(
        f"static const char *target = {cstr(executable)};\n" +
        "static const char *fixed[] = {" + ','.join(cstr(x) for x in fixed) + ",NULL};\n" +
        f"static const char *audit_path = {cstr(str(audit))};\n" +
        f"static const char *map_path = {cstr(str(mapfile))};\n" +
        f"static const char *workdir = {cstr(cwd) if cwd else 'NULL'};\n" +
        "static const struct check checks[] = {" + ','.join(stamp(p) for p in checks) + "};\n" +
        "static const char *env_keys[] = {" + ','.join(cstr(k) for k in env) + ("," if env else "") + "NULL};\n" +
        "static const char *env_values[] = {" + ','.join(cstr(v) for v in env.values()) + ("," if env else "") + "NULL};\n")
    tmp = out / "entry.tmp"
    run([tool("cc"), "-O2", "-Wall", "-Wextra", "-I", out, NATIVE / "launcher.c", "-o", tmp],
        out / "launcher.build.log")
    # Atomic publication refuses overwrite, including races.
    os.link(tmp, entry)
    tmp.unlink()
    return str(entry)
