#!/usr/bin/env python3
"""Build an AppDir and a type-2 AppImage, including Python and native tool closure."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tgo.runtime import build_runtime
from tgo.util import sha256, write_json, tool
from bootstrap import fetch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=ROOT / "dist")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--tool", action="append", default=[], metavar="NAME=PATH")
    p.add_argument("--qemu-header", default=os.environ.get("TGO_QEMU_HEADER"))
    p.add_argument("--appimagetool")
    p.add_argument("--appdir-only", action="store_true")
    a = p.parse_args()
    dest = a.output.resolve(); dest.mkdir(parents=True, exist_ok=True)
    app = dest / "TGO.AppDir"
    app.mkdir(exist_ok=False)
    binpath, libpath = app / "usr/bin", app / "usr/lib"
    binpath.mkdir(parents=True); libpath.mkdir()
    patch = tool("patchelf")
    explicit = dict(s.split("=", 1) for s in a.tool)
    required = ("llvm-bolt", "qemu-" + platform.machine(), "patchelf")
    executables = {n: tool(n, explicit.get(n)) for n in required}
    for n in ("perf", "merge-fdata"):
        found = tool(n, explicit.get(n), required=False)
        if found:
            executables[n] = found
    executables["python3"] = str(Path(a.python).resolve())
    manifest = {"schema": "tgo.package.v1", "arch": platform.machine(), "tools": {}, "libraries": {}}
    native = app / "usr/lib/tgo-runtime"
    build_runtime(native, a.qemu_header)
    if not (native / "libtgo_qemu.so").exists():
        raise ValueError("--qemu-header required to package a matching QEMU plugin")
    shutil.copytree(ROOT / "tgo", app / "usr/tgo", ignore=shutil.ignore_patterns("__pycache__"))
    info = json.loads(subprocess.check_output([a.python, "-c",
        "import sys,sysconfig,json;print(json.dumps({'stdlib':sysconfig.get_path('stdlib'),'dynload':sysconfig.get_config_var('DESTSHARED'),'version':str(sys.version_info.major)+'.'+str(sys.version_info.minor)}))"], text=True))
    pylib = libpath / ("python" + info["version"])
    shutil.copytree(info["stdlib"], pylib, ignore=shutil.ignore_patterns(
        "site-packages", "dist-packages", "test", "tests", "__pycache__", "*.pyc", "idlelib", "tkinter", "ensurepip"))
    if info["dynload"] and Path(info["dynload"]).is_dir() and not (pylib / "lib-dynload").exists():
        shutil.copytree(info["dynload"], pylib / "lib-dynload")
    copied = {}
    queue = []
    for name, src in executables.items():
        target = binpath / name
        shutil.copy2(src, target)
        if target.read_bytes()[:4] != b"\x7fELF":
            raise ValueError(f"packaging requires the real executable, not a shell wrapper: {src}")
        manifest["tools"][name] = {"source": src, "sha256": sha256(src)}
        queue.append(target)
    (binpath / "perf2bolt").symlink_to("llvm-bolt")
    queue += list(pylib.rglob("*.so")) + list(native.glob("*.so")) + [native / "launcher"]
    # The loader and libc stay host supplied. The build host sets the glibc floor;
    # every other ldd dependency is copied and given a relative runtime search path.
    base = re.compile(r"^(libc\.so|libm\.so|libpthread\.so|libdl\.so|librt\.so|ld-linux|linux-vdso|libresolv\.so)")
    all_elfs = []
    while queue:
        binary = queue.pop()
        all_elfs.append(binary)
        r = subprocess.run(["ldd", str(binary)], capture_output=True, text=True)
        if "not found" in r.stdout:
            raise ValueError(f"unresolved shared library in {binary}:\n{r.stdout}")
        for line in r.stdout.splitlines():
            match = re.search(r"^\s*(\S+)\s+=>\s+(/\S+)", line)
            if not match:
                continue
            soname, source = match.groups()
            if base.match(soname):
                continue
            digest = sha256(source)
            if soname in copied:
                if copied[soname] != digest:
                    raise ValueError(f"conflicting bundled soname: {soname}")
                continue
            target = libpath / soname
            shutil.copy2(source, target)
            copied[soname] = digest
            manifest["libraries"][soname] = {"source": source, "sha256": digest}
            queue.append(target)
    for binary in all_elfs:
        r = subprocess.run([patch, "--print-rpath", str(binary)], capture_output=True)
        if r.returncode == 0:
            relative = os.path.relpath(libpath, binary.parent)
            subprocess.run([patch, "--set-rpath", "$ORIGIN/" + relative, str(binary)], check=True)
    # Audit/ROI helpers copied into workload bundles must load against host libc.
    # QEMU's plugin is libc-only too; it needs no application-private libraries.
    apprun = app / "AppRun"
    apprun.write_text('''#!/bin/sh
set -eu
TGO_APPDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export TGO_PACKAGED=1
export TGO_SAVED_PATH="${PATH-__UNSET__}"
export TGO_SAVED_PYTHONHOME="${PYTHONHOME-__UNSET__}"
export TGO_SAVED_PYTHONPATH="${PYTHONPATH-__UNSET__}"
export PATH="$TGO_APPDIR/usr/bin:${PATH-}"
export PYTHONHOME="$TGO_APPDIR/usr"
export PYTHONPATH="$TGO_APPDIR/usr"
export TGO_RUNTIME_DIR="$TGO_APPDIR/usr/lib/tgo-runtime"
exec "$TGO_APPDIR/usr/bin/python3" -m tgo "$@"
''')
    apprun.chmod(0o755)
    (app / "tgo.desktop").write_text("[Desktop Entry]\nType=Application\nName=TGO\nExec=AppRun\nIcon=tgo\nCategories=Development;\nTerminal=true\n")
    (app / "tgo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" rx="24" fill="#15394a"/><path d="M20 85L50 55 72 70 108 30" fill="none" stroke="#64ddad" stroke-width="12"/></svg>')
    shutil.copy2(ROOT / "dependencies.lock.json", app / "dependencies.lock.json")
    if (ROOT / "docs/THIRD_PARTY.md").exists():
        shutil.copy2(ROOT / "docs/THIRD_PARTY.md", app / "THIRD_PARTY.md")
    manifest["glibc_build_host"] = subprocess.check_output(["ldd", "--version"], text=True).splitlines()[0]
    write_json(app / "package-manifest.json", manifest)
    subprocess.run([apprun, "--help"], check=True)
    if not a.appdir_only:
        image_tool = Path(a.appimagetool) if a.appimagetool else fetch("appimagetool-" + platform.machine(), ROOT / "deps")
        image_tool.chmod(0o755)
        tool_dir = dest / "appimagetool-extracted"
        tool_dir.mkdir(exist_ok=True)
        subprocess.run([image_tool.resolve(), "--appimage-extract"], cwd=tool_dir, check=True, stdout=subprocess.DEVNULL)
        image = dest / ("tgo-0.1.0-" + platform.machine() + ".AppImage")
        env = os.environ.copy(); env["ARCH"] = platform.machine()
        runtime = fetch("runtime-" + platform.machine(), ROOT / "deps")
        subprocess.run([tool_dir / "squashfs-root/AppRun", "--no-appstream", "--runtime-file", runtime, app, image], env=env, check=True)
        write_json(image.with_suffix(".sha256.json"), {"file": image.name, "sha256": sha256(image)})
        print(image)


if __name__ == "__main__":
    main()
