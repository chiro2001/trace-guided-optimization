#!/usr/bin/env python3
"""Exercise Go capture, layout and an independent entry with no C compiler in PATH."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appimage", type=Path)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    image, go = args.appimage.resolve(), args.go.absolute()
    minimal = root / "path"; minimal.mkdir()
    for name in ("sh", "dirname", "env", "mkdir", "rm", "cat", "true", "readlink"):
        source = shutil.which(name)
        if source:
            (minimal / name).symlink_to(source)
    env = os.environ.copy(); env["PATH"] = str(minimal)
    env.pop("TGO_DISABLE", None)
    command = [str(image), "--appimage-extract-and-run"]
    doctor = json.loads(subprocess.check_output([*command, "doctor"], env=env, text=True))
    if doctor["tools"]["cc"] is not None:
        raise RuntimeError("smoke environment unexpectedly contains a C compiler")
    (root / "doctor.json").write_text(json.dumps(doctor, indent=2)+"\n")
    source = root / "fixture.go"
    source.write_text("package fixture\n" + ''.join(
        f"func F{i}(x int) int {{for j:=0;j<50;j++{{x=(x*33+j)^(x>>3)}};return x+{i}}}\n"
        for i in range(8000)))
    bundle = root / "bundle"
    # This is a functionality smoke test, not evidence for performance selection.
    with (root / "tune.log").open("w") as log:
        subprocess.run([*command, "tune", "--strategy", "go-compiler", "--go", str(go),
            "--go-variant", "layout-runtime", "--search-pairs", "1", "--validate", "1",
            "--go-warmups", "0", "--minimum-gain", "100", "--timeout", "900",
            "-o", str(bundle), "--", "go", "build", "-p", "1", "-o", "/dev/null", str(source)],
            env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    candidate = bundle / "layout-runtime"
    built = json.loads((candidate / "build.json").read_text())
    if not built.get("validation", {}).get("equivalent"):
        raise RuntimeError("Go AppImage candidate failed correctness/execution validation")
    logpath = root / "independent-tools.tsv"
    env.update(GOCACHE=str(root / "independent-cache"), TGO_GO_EXEC_LOG=str(logpath))
    with tempfile.TemporaryDirectory(prefix="tgo-go-outside-") as outside:
        subprocess.run([built["entry"]], cwd=outside, env=env, check=True, timeout=900)
    compiler = built["go_compiler"]["optimized"]
    if compiler not in logpath.read_text():
        raise RuntimeError("independent entry did not execute the optimized compiler")
    digest = hashlib.sha256()
    with image.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    report = {"schema": "tgo.smoke.go-appimage.v1", "passed": True,
        "appimage": str(image), "appimage_sha256": digest.hexdigest(),
        "go": str(go), "c_compiler_in_path": None, "source": str(source),
        "candidate_entry": built["entry"], "optimized_compiler": compiler,
        "independent_cwd": "temporary directory outside bundle", "execution_log": str(logpath),
        "layout": built["go_compiler"]["layout"]["verification"],
        "limits": "functionality smoke only; one screening pair is not performance evidence"}
    (root / "smoke.json").write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
