#!/usr/bin/env python3
"""Download pinned sources, verify SHA256, build tools in a private prefix."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import urllib.error
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tgo.util import sha256, write_json


def fetch(name, prefix):
    item = json.loads((ROOT / "dependencies.lock.json").read_text())[name]
    dest = prefix / "downloads" / item["url"].rsplit("/", 1)[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or sha256(dest) != item["sha256"]:
        temp = dest.with_suffix(dest.suffix + ".part")
        print("download", item["url"], flush=True)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(item["url"], timeout=60) as response, temp.open("wb") as output:
                    shutil.copyfileobj(response, output, 1024 * 1024)
                break
            except (urllib.error.URLError, OSError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        if sha256(temp) != item["sha256"]:
            raise ValueError(f"SHA256 mismatch: {temp}")
        temp.replace(dest)
    return dest


def unpack(name, prefix):
    archive = fetch(name, prefix)
    sources = prefix / "sources"; sources.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        top = tar.getmembers()[0].name.split("/")[0]
        dest = sources / top
        if not dest.exists():
            # Do not allow archive entries or symlinks to escape the source root.
            members = []
            for m in tar.getmembers():
                target = (sources / m.name).resolve()
                if sources not in target.parents:
                    raise ValueError("unsafe source archive path")
                if m.issym() or m.islnk():
                    parent = target.parent if m.issym() else sources
                    linked = (parent / m.linkname).resolve()
                    if sources not in linked.parents:
                        if name == "qemu" and m.name.endswith("roms/edk2/EmulatorPkg/Unix/Host/X11IncludeHack") and m.linkname == "/opt/X11/include":
                            print("omit unused Darwin edk2 SDK symlink:", m.name, flush=True)
                            continue
                        raise ValueError("unsafe source archive link")
                members.append(m)
            tar.extractall(sources, members=members)
    return dest


def execute(command, cwd=None, env=None):
    print("run", json.dumps([str(c) for c in command]), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", type=Path, default=ROOT / "deps")
    p.add_argument("--components", nargs="+", choices=["llvm", "qemu", "python", "glib", "elfutils", "perf", "patchelf", "appimagetool"], default=["llvm", "qemu", "perf", "patchelf"])
    p.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    p.add_argument("--download-only", action="store_true")
    a = p.parse_args()
    a.prefix = a.prefix.resolve(); a.prefix.mkdir(parents=True, exist_ok=True)
    install = a.prefix / "install"; (install / "bin").mkdir(parents=True, exist_ok=True)
    components = a.components
    if a.download_only:
        for c in components:
            import platform
            fetch("appimagetool-" + platform.machine() if c == "appimagetool" else c, a.prefix)
        return
    buildenv = a.prefix / "build-venv"
    if not buildenv.exists():
        execute([sys.executable, "-m", "venv", buildenv])
    # Build frontend wheels are version pinned; the optimization source archives
    # and distributed executables are additionally SHA256 pinned in the lockfile.
    execute([buildenv / "bin/pip", "install", "cmake==3.31.6", "ninja==1.11.1.3", "meson==1.7.2"])
    env = os.environ.copy()
    env["PATH"] = str(buildenv / "bin") + os.pathsep + str(install / "bin") + os.pathsep + env["PATH"]
    env["PKG_CONFIG_PATH"] = str(install / "lib/pkgconfig") + ':' + str(install / "lib64/pkgconfig") + ':' + env.get("PKG_CONFIG_PATH", "")
    env["LD_LIBRARY_PATH"] = str(install / "lib") + ':' + str(install / "lib64") + ':' + env.get("LD_LIBRARY_PATH", "")
    if "qemu" in components and "glib" not in components:
        have_glib = subprocess.run(["pkg-config", "--exists", "glib-2.0>=2.66"], env=env).returncode == 0
        if not have_glib:
            components = ["glib", *components]
    if "perf" in components and "elfutils" not in components:
        if subprocess.run(["pkg-config", "--exists", "libelf"], env=env).returncode:
            components = ["elfutils", *components]
    for component in components:
        if component == "appimagetool":
            import platform
            path = fetch("appimagetool-" + platform.machine(), a.prefix)
            target = install / "bin/appimagetool"; shutil.copy2(path, target); target.chmod(0o755)
            continue
        source = unpack(component, a.prefix)
        build = a.prefix / "build" / component
        build.parent.mkdir(parents=True, exist_ok=True)
        if component == "llvm":
            marker = source / ".tgo-pages-patch"
            patch = ROOT / "patches/bolt22-aarch64-pages.patch"
            if not marker.exists():
                execute(["patch", "-p1", "-i", patch], cwd=source)
                marker.write_text(sha256(patch))
            elif marker.read_text() != sha256(patch):
                raise ValueError("LLVM patch changed; use a fresh source prefix")
            execute(["cmake", "-S", source / "llvm", "-B", build, "-G", "Ninja",
                     "-DCMAKE_BUILD_TYPE=Release", "-DLLVM_ENABLE_PROJECTS=bolt",
                     "-DLLVM_TARGETS_TO_BUILD=AArch64;X86", "-DLLVM_ENABLE_ASSERTIONS=ON",
                     "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF",
                     "-DLLVM_INCLUDE_EXAMPLES=OFF", "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF",
                     "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_CURL=OFF", "-DLLVM_ENABLE_LIBEDIT=OFF"], env=env)
            execute(["cmake", "--build", build, "--target", "llvm-bolt", "merge-fdata", "-j", a.jobs], env=env)
            for name in ("llvm-bolt", "merge-fdata"):
                shutil.copy2(build / "bin" / name, install / "bin" / name)
            p2b = install / "bin/perf2bolt"
            if not p2b.exists():
                p2b.symlink_to("llvm-bolt")
        elif component == "glib":
            if not (build / "build.ninja").exists():
                execute(["meson", "setup", build, source, "--prefix=" + str(install), "--libdir=lib",
                         "--buildtype=release", "-Dtests=false", "-Dinstalled_tests=false",
                         "-Ddocumentation=false", "-Dman-pages=disabled", "-Dintrospection=disabled",
                         "-Dlibmount=disabled", "-Dselinux=disabled", "-Dxattr=false"], env=env)
            execute(["ninja", "-C", build, "-j", a.jobs], env=env)
            execute(["ninja", "-C", build, "install"], env=env)
        elif component == "qemu":
            build.mkdir(exist_ok=True)
            execute([source / "configure", "--prefix=" + str(install),
                     "--target-list=aarch64-linux-user,x86_64-linux-user", "--enable-plugins",
                     "--disable-system", "--disable-docs", "--disable-tools", "--disable-werror"], cwd=build, env=env)
            execute(["ninja", "-j", a.jobs], cwd=build, env=env)
            for name in ("qemu-aarch64", "qemu-x86_64"):
                shutil.copy2(build / name, install / "bin" / name)
            (install / "include").mkdir(exist_ok=True)
            shutil.copy2(source / "include/qemu/qemu-plugin.h", install / "include/qemu-plugin.h")
        elif component == "elfutils":
            build.mkdir(exist_ok=True)
            elfenv = env.copy()
            elfenv["CPPFLAGS"] = "-I" + str(install / "include")
            elfenv["LDFLAGS"] = "-L" + str(install / "lib") + " -Wl,-rpath," + str(install / "lib")
            execute([source / "configure", "--prefix=" + str(install), "--libdir=" + str(install / "lib"),
                     "--disable-debuginfod", "--disable-libdebuginfod", "--disable-nls",
                     "--without-bzlib", "--without-lzma", "--without-zstd"], cwd=build, env=elfenv)
            execute(["make", "-j", a.jobs], cwd=build, env=elfenv)
            execute(["make", "install"], cwd=build, env=elfenv)
        elif component == "perf":
            build.mkdir(exist_ok=True)
            perfenv = env.copy()
            # perf's feature probes use LDFLAGS before EXTRA_LDFLAGS is merged.
            perfenv["LDFLAGS"] = "-L" + str(install / "lib") + " -Wl,-rpath," + str(install / "lib")
            flags = ["NO_LIBPERL=1", "NO_LIBPYTHON=1", "NO_SLANG=1", "NO_GTK2=1",
                     "NO_LIBUNWIND=1", "NO_LIBDW_DWARF_UNWIND=1", "NO_DWARF=1",
                     "NO_LIBBPF=1", "NO_LIBTRACEEVENT=1", "NO_LIBNUMA=1", "NO_LIBCAP=1",
                     "NO_LIBCRYPTO=1", "NO_LIBZSTD=1", "NO_LIBBFD=1", "NO_JVMTI=1",
                     "NO_LIBPFM4=1", "WERROR=0", "EXTRA_CFLAGS=-I" + str(install / "include"),
                     "EXTRA_LDFLAGS=-L" + str(install / "lib") + " -Wl,-rpath," + str(install / "lib")]
            execute(["make", "-C", source / "tools/perf", "O=" + str(build) + "/", "-j", a.jobs, *flags], env=perfenv)
            shutil.copy2(build / "perf", install / "bin/perf")
        elif component == "patchelf":
            build.mkdir(exist_ok=True)
            execute([source / "configure", "--prefix=" + str(install)], cwd=build, env=env)
            execute(["make", "-j", a.jobs], cwd=build, env=env)
            execute(["make", "install"], cwd=build, env=env)
        elif component == "python":
            build.mkdir(exist_ok=True)
            execute([source / "configure", "--prefix=" + str(install), "--enable-shared", "--without-ensurepip",
                     "CFLAGS=-O3 -fno-omit-frame-pointer", "LDFLAGS=-Wl,--emit-relocs -Wl,-rpath," + str(install / "lib")], cwd=build, env=env)
            execute(["make", "-j", a.jobs], cwd=build, env=env)
            execute(["make", "install"], cwd=build, env=env)
    write_json(a.prefix / "build-manifest.json", {"components": components,
               "lock_sha256": sha256(ROOT / "dependencies.lock.json"),
               "binaries": {p.name: sha256(p) for p in (install / "bin").iterdir() if p.is_file()}})


if __name__ == "__main__":
    main()
