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
from tgo import __version__
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
    for n in ("perf", "merge-fdata", "llvm-bolt-recover"):
        found = tool(n, explicit.get(n), required=False)
        if found:
            executables[n] = found
    executables["python3"] = str(Path(a.python).resolve())
    manifest = {"schema": "tgo.package.v1", "version": __version__, "arch": platform.machine(), "tools": {}, "libraries": {}}
    manifest["source_commit"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    manifest["source_dirty"] = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip())
    manifest["dependency_lock_sha256"] = sha256(ROOT / "dependencies.lock.json")
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
    # Each tool has a private dependency closure. QEMU and perf may carry
    # different builds of the same SONAME; they must not contaminate each other.
    base = re.compile(r"^(libc\.so|libm\.so|libpthread\.so|libdl\.so|librt\.so|ld-linux|linux-vdso|libresolv\.so)")
    def closure(label, primary, library_dir, extras=()):
        copied, queue, all_elfs = {}, [], []
        library_dir.mkdir(parents=True, exist_ok=True)
        resolve_env = os.environ.copy()
        search = [str(library_dir)]
        for source, _ in primary.values():
            rpath = subprocess.check_output([patch, "--print-rpath", source], text=True).strip()
            origin = str(Path(source).parent)
            search += [p.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
                       for p in rpath.split(":") if p]
        if resolve_env.get("LD_LIBRARY_PATH"):
            search.append(resolve_env["LD_LIBRARY_PATH"])
        resolve_env["LD_LIBRARY_PATH"] = ':'.join(search)
        for name, (source, target) in primary.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if target.read_bytes()[:4] != b"\x7fELF":
                raise ValueError(f"requires a real executable rather than a shell wrapper: {source}")
            manifest["tools"][name] = {"source": source, "sha256": sha256(source),
                                      "packaged": str(target.relative_to(app))}
            queue.append(target)
        queue += list(extras)
        while queue:
            binary = queue.pop(); all_elfs.append(binary)
            # Resolve an extension/dependency in its executable's search context.
            # ldd on a lone libcrypto, for example, otherwise loses Python's
            # private zlib path and reports a conflicting system build.
            r = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, env=resolve_env)
            if "not found" in r.stdout:
                raise ValueError(f"unresolved dependency in {binary}:\n{r.stdout}")
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
                        raise ValueError(f"conflicting SONAME within {label}: {soname}")
                    continue
                target = library_dir / soname
                shutil.copy2(source, target); copied[soname] = digest
                manifest["libraries"][label + "/" + soname] = {"source": source, "sha256": digest}
                queue.append(target)
        for binary in all_elfs:
            r = subprocess.run([patch, "--print-rpath", str(binary)], capture_output=True)
            if r.returncode == 0:
                relative = os.path.relpath(library_dir, binary.parent)
                subprocess.run([patch, "--set-rpath", "$ORIGIN/" + relative, str(binary)], check=True)
    closure("python", {"python3": (executables.pop("python3"), binpath / "python3")}, libpath,
            list(pylib.rglob("*.so")) + list(native.glob("*.so")) + [native / "launcher", native / "go-tool"])
    for name, source in executables.items():
        private = app / "usr/tools" / name
        closure(name, {name: (source, private / "bin" / name)}, private / "lib")
        wrapper = binpath / name
        wrapper.write_text('#!/bin/sh\nTGO_TOOL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../tools/' + name + '/bin" && pwd)\nexec "$TGO_TOOL_DIR/' + name + '" "$@"\n')
        wrapper.chmod(0o755)
        if name == "llvm-bolt-recover":
            (private / "bin/perf2bolt").symlink_to(name)
    (app / "usr/tools/llvm-bolt/bin/perf2bolt").symlink_to("llvm-bolt")
    (binpath / "perf2bolt").write_text('#!/bin/sh\nTGO_TOOL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../tools/llvm-bolt/bin" && pwd)\nexec "$TGO_TOOL_DIR/perf2bolt" "$@"\n')
    (binpath / "perf2bolt").chmod(0o755)
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
export TGO_SAVED_PYTHONPLATLIBDIR="${PYTHONPLATLIBDIR-__UNSET__}"
export PATH="$TGO_APPDIR/usr/bin:${PATH-}"
export PYTHONHOME="$TGO_APPDIR/usr"
export PYTHONPATH="$TGO_APPDIR/usr"
export PYTHONPLATLIBDIR=lib
export TGO_RUNTIME_DIR="$TGO_APPDIR/usr/lib/tgo-runtime"
export TGO_TOOL_ROOT="$TGO_APPDIR/usr/tools"
exec "$TGO_APPDIR/usr/bin/python3" -m tgo "$@"
''')
    apprun.chmod(0o755)
    (app / "tgo.desktop").write_text("[Desktop Entry]\nType=Application\nName=TGO\nExec=AppRun\nIcon=tgo\nCategories=Development;\nTerminal=true\n")
    (app / "tgo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" rx="24" fill="#15394a"/><path d="M20 85L50 55 72 70 108 30" fill="none" stroke="#64ddad" stroke-width="12"/></svg>')
    shutil.copy2(ROOT / "dependencies.lock.json", app / "dependencies.lock.json")
    notices = app / "usr/share/doc/tgo/licenses"
    notices.mkdir(parents=True)
    source_root = ROOT / "deps/sources"
    source_dirs = list(source_root.iterdir()) if source_root.exists() else []
    recovery_sources = source_root / "llvm-recover"
    if recovery_sources.is_dir():
        source_dirs += [p for p in recovery_sources.iterdir() if p.is_dir()]
    for tree in list(source_dirs):
        sub = tree / "subprojects"
        if sub.is_dir():
            source_dirs += [p for p in sub.iterdir() if p.is_dir() and p.name.startswith(("pcre2", "libffi", "zlib"))]
    manifest["license_files"] = []
    for tree in source_dirs:
        files = {f for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "COPYRIGHT*") for f in tree.glob(pattern) if f.is_file()}
        if tree.name.startswith("zlib") and (tree / "README").exists():
            files.add(tree / "README")
        if tree.name.startswith("linux-"):
            files.update(f for f in (tree / "LICENSES/preferred").glob("GPL-2.0") if f.is_file())
        for f in files:
            target = notices / tree.name / f.name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(f, target)
            manifest["license_files"].append(str(target.relative_to(app)))
    if (ROOT / "docs/THIRD_PARTY.md").exists():
        shutil.copy2(ROOT / "docs/THIRD_PARTY.md", app / "THIRD_PARTY.md")
    shutil.copytree(ROOT / "patches", app / "usr/share/doc/tgo/patches")
    manifest["glibc_build_host"] = subprocess.check_output(["ldd", "--version"], text=True).splitlines()[0]
    write_json(app / "package-manifest.json", manifest)
    subprocess.run([apprun, "--help"], check=True)
    if not a.appdir_only:
        image_tool = Path(a.appimagetool) if a.appimagetool else fetch("appimagetool-" + platform.machine(), ROOT / "deps")
        image_tool.chmod(0o755)
        tool_dir = dest / "appimagetool-extracted"
        tool_dir.mkdir(exist_ok=True)
        subprocess.run([image_tool.resolve(), "--appimage-extract"], cwd=tool_dir, check=True, stdout=subprocess.DEVNULL)
        image = dest / ("tgo-" + __version__ + "-" + platform.machine() + ".AppImage")
        runtime = fetch("runtime-" + platform.machine(), ROOT / "deps")
        runtime.chmod(0o755)
        offset = int(subprocess.check_output([runtime, "--appimage-offset"], text=True).strip())
        if offset < runtime.stat().st_size:
            raise ValueError("runtime ELF offset is shorter than its file")
        squash = shutil.which("mksquashfs")
        if not squash:
            squash = next(iter((tool_dir / "squashfs-root").rglob("mksquashfs")), None)
        if not squash:
            raise ValueError("mksquashfs missing from system and extracted build tool")
        filesystem = dest / "tgo.squashfs"
        env = os.environ.copy()
        subprocess.run([str(squash), str(app), str(filesystem), "-noappend", "-all-root", "-processors", "8"], env=env, check=True)
        with image.open("wb") as output:
            with runtime.open("rb") as stream:
                shutil.copyfileobj(stream, output)
            output.write(b"\0" * (offset - runtime.stat().st_size))
            with filesystem.open("rb") as stream:
                shutil.copyfileobj(stream, output)
        image.chmod(0o755)
        subprocess.run([image, "--appimage-extract-and-run", "--help"], check=True)
        write_json(image.with_suffix(".sha256.json"), {"file": image.name, "sha256": sha256(image)})
        print(image)


if __name__ == "__main__":
    main()
