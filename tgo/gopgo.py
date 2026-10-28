"""Go compiler PGO: the Go linker owns all PC, stack-map and GC metadata updates."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from .runtime import build_runtime, make_launcher
from .util import command_path, read_json, write_json, sha256, tool, workload_env
from .validate import execute_trial, validate


def file_stamp(path):
    st = Path(path).stat()
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns // 10**9}:{st.st_mtime_ns % 10**9}"


def tool_id(version, identity):
    return "buildID=tgo/" + identity if "devel" in version.split()[2] else "tgo=" + identity


def profile_totals(text):
    active = False
    count, ns = 0, 0
    for line in text.splitlines():
        if line == "Samples:":
            active = True
        elif line.startswith("Locations"):
            break
        elif active:
            m = re.match(r"^\s+(\d+)\s+(\d+):", line)
            if m:
                count += int(m[1]); ns += int(m[2])
    if not count or not ns:
        raise ValueError("Go CPU profile has no usable samples; inspect profile.raw.txt")
    return {"samples": count, "cpu_nanoseconds": ns}


def go_command(args, go, env, root, log, timeout=1800, cwd=None):
    command = [go, *map(str, args)]
    code, seconds, timed_out, digest = execute_trial(command, root / log, env, cwd or os.getcwd(), timeout)
    record = {"command": command, "returncode": code, "seconds": seconds,
              "timed_out": timed_out, "stdout_sha256": digest,
              "stdout": str(root / (log + ".stdout")), "stderr": str(root / (log + ".stderr"))}
    if code or timed_out:
        tail = (root / (log + ".stderr")).read_text(errors="replace")[-2000:]
        raise RuntimeError(f"Go tool failed; see {root / (log + '.stderr')}\n{tail}")
    return record


def adapter_env(base, runtime, compiler, version, identity, optimized=None):
    env = dict(base)
    flags = env.get("GOFLAGS", "")
    if any(p == "-toolexec" or p.startswith("-toolexec=") for p in shlex.split(flags)):
        raise ValueError("GOFLAGS already specifies -toolexec; provide an unwrapped Go workload")
    # Go's GOFLAGS and -toolexec have separate quoting grammars. Use an explicit
    # supported path domain rather than silently changing arguments with spaces.
    wrapper = str(Path(runtime) / "go-tool")
    if any(c.isspace() or c in "'\"" for c in wrapper):
        raise ValueError("Go adapter output directory cannot contain whitespace or quote characters")
    env.update(GOFLAGS=(flags + " -toolexec=" + wrapper).strip(),
               TGO_GO_ORIGINAL=compiler, TGO_GO_ORIGINAL_STAMP=file_stamp(compiler),
               TGO_GO_VERSION=version, TGO_GO_ID=tool_id(version, identity))
    if optimized:
        env.update(TGO_GO_COMPILER=str(optimized), TGO_GO_COMPILER_STAMP=file_stamp(optimized))
    return env


def correctness_fixture(go, env, root, timeout):
    fixture = Path(__file__).parent / "fixtures/go_correctness.go"
    target = root / "correctness-program"
    command = go_command(["build", "-o", target, fixture], go, env, root, "correctness-build", timeout)
    code, seconds, timed_out, digest = execute_trial([str(target)], root / "correctness-run", env, str(root), timeout)
    output = (root / "correctness-run.stdout").read_text()
    if code or timed_out or output != "go-correctness-ok\n":
        raise ValueError(f"Go compiler correctness fixture failed: {root}")
    return {"build": command, "run_seconds": seconds, "stdout_sha256": digest}


def tune_go(args, root_exists=False):
    root = Path(args.output).absolute()
    root.mkdir(parents=True, exist_ok=root_exists)
    previous = Path(args.run).resolve() if args.run else None
    prior_state = read_json(previous / "run.json") if previous else None
    prior_profile = read_json(previous / "profile.json") if previous else None
    if prior_state and (prior_state.get("backend") != "go-cpu" or prior_state.get("state") != "recorded"):
        raise ValueError("Go PGO reuse requires a completed go-cpu capture")
    command = command_path(args.command or (prior_state["command"] if prior_state else []))
    overrides = dict(prior_state["env"]) if prior_state else {}
    overrides.update(dict(item.split("=", 1) for item in args.env))
    base = workload_env(); base.update(overrides)
    prior_go = str(Path(prior_state["toolchain"]["GOROOT"]) / "bin/go") if prior_state else None
    go = tool("go", args.go or prior_go)
    if Path(command[0]).name == "go":
        if args.go:
            command[0] = go
        else:
            go = command[0]
    # Shell drivers that invoke `go` must see the selected toolchain too.
    base["PATH"] = str(Path(go).parent) + os.pathsep + base.get("PATH", "")
    overrides["PATH"] = base["PATH"]
    for key in list(base):
        if key.startswith("TGO_GO_"):
            base.pop(key)
    info = json.loads(subprocess.check_output([go, "env", "-json", "GOROOT", "GOTOOLDIR", "GOVERSION",
        "GOOS", "GOARCH", "GOHOSTOS", "GOHOSTARCH", "GOEXPERIMENT", "CGO_ENABLED"], env=base, text=True))
    compiler = str((Path(info["GOTOOLDIR"]) / "compile").resolve())
    goroot = Path(info["GOROOT"])
    if not (goroot / "src/cmd/compile/main.go").exists():
        raise ValueError("selected Go toolchain does not contain cmd/compile source")
    version = subprocess.check_output([compiler, "-V=full"], env=base, text=True).strip()
    original_sha = sha256(compiler)
    result = {"schema": "tgo.tune.v1", "strategy": "go-compiler-pgo", "selected": "baseline",
              "toolchain": info, "go": {"path": go, "sha256": sha256(go)},
              "compiler": {"path": compiler, "sha256": original_sha, "version": version},
              "candidates": [], "state": "training"}
    write_json(root / "tune.json", result)
    try:
        runtime = build_runtime(root / "runtime")
        if not (runtime / "go-tool").exists():
            raise ValueError("Go tool adapter absent from this package; rebuild TGO")
        capture = root / "capture"; capture.mkdir()
        profiles = capture / "profiles"; profiles.mkdir()
        state = {"schema": "tgo.run.v1", "strategy": "go-compiler-pgo", "backend": "go-cpu",
                 "command": command, "cwd": os.getcwd(), "env": overrides, "state": "recording",
                 "validation": {"go_cache": args.go_cache}, "toolchain": info}
        write_json(capture / "run.json", state)
        if previous:
            if prior_profile["compiler_sha256"] != original_sha:
                raise ValueError("Go CPU profiles belong to a different compiler")
            for item in prior_profile["profiles"]:
                path = Path(item["path"])
                if sha256(path) != item["sha256"]:
                    raise ValueError("raw Go CPU profile changed")
                shutil.copy2(path, profiles / path.name)
            state.update(returncode=0, training_origin=str(previous),
                         training_manifest_sha256=sha256(previous / "profile.json"))
        else:
            training = adapter_env(base, runtime, compiler, version, "profile-" + original_sha[:16])
            training.update(TGO_GO_PROFILE_DIR=str(profiles), TGO_GO_EXEC_LOG=str(capture / "tools.tsv"),
                            GOCACHE=str(capture / "cache"))
            code, seconds, timed_out, digest = execute_trial(command, capture / "workload", training, state["cwd"], args.timeout)
            state.update(returncode=code, seconds=seconds, timed_out=timed_out, stdout_sha256=digest)
            if code or timed_out:
                raise ValueError(f"Go profiling workload failed; inspect {capture / 'workload.stderr'}")
        files = sorted(p for p in profiles.glob("*.pprof") if p.stat().st_size)
        if not files:
            raise ValueError("no compiler profiles produced; check selected --go and workload cache behavior")
        merged = capture / "merged.pprof"
        merge = go_command(["tool", "pprof", "-proto", "-symbolize=none", "-output", merged, *files], go, base, capture, "merge", args.timeout)
        raw = go_command(["tool", "pprof", "-raw", "-symbolize=none", merged], go, base, capture, "profile", args.timeout)
        totals = profile_totals(Path(raw["stdout"]).read_text())
        preprocessed = go_command(["tool", "preprofile", "-i", merged, "-o", capture / "profile.preprocessed"],
                                 go, base, capture, "preprofile-check", args.timeout)
        if sha256(compiler) != original_sha:
            raise ValueError("original compiler changed during training")
        write_json(capture / "profile.json", {"schema": "tgo.go-profile.v1", "compiler_sha256": original_sha,
                   "profile": str(merged), "sha256": sha256(merged), "totals": totals,
                   "profiles": [{"path": str(p), "sha256": sha256(p)} for p in files], "merge": merge,
                   "metadata_validation": preprocessed})
        state.update(state="recorded", phase="complete")
        write_json(capture / "run.json", state)
        shutil.rmtree(capture / "cache", ignore_errors=True)
        result.update(capture=str(capture), training=totals, state="building")
        write_json(root / "tune.json", result)
        candidate = root / "pgo"; candidate.mkdir()
        build_runtime(candidate)
        optimized = candidate / "compile"
        buildenv = dict(base)
        buildenv.update(GOFLAGS="", GOOS=info["GOHOSTOS"], GOARCH=info["GOHOSTARCH"], GOTOOLCHAIN="local")
        buildenv.pop("TGO_DISABLE", None)
        compilation = go_command(["build", "-pgo=" + str(merged), "-o", optimized, "cmd/compile"],
                                 go, buildenv, candidate, "compiler-build", args.timeout)
        actual_version = subprocess.check_output([optimized, "-V=full"], env=base, text=True).strip()
        if actual_version != version:
            raise ValueError(f"rebuilt compiler version differs: {actual_version!r} != {version!r}")
        digest = sha256(optimized)
        pgo_base = dict(overrides)
        pgo_base["GOFLAGS"] = base.get("GOFLAGS", "")
        changed = adapter_env(pgo_base, runtime, compiler, version, digest, optimized)
        # Build a representative semantics fixture using the candidate compiler.
        checkenv = dict(base); checkenv.update(changed)
        checkenv.update(GOOS=info["GOHOSTOS"], GOARCH=info["GOHOSTARCH"],
                        GOCACHE=str(candidate / "correctness-cache"), TGO_GO_EXEC_LOG=str(candidate / "correctness-tools.tsv"))
        fixture = correctness_fixture(go, checkenv, candidate, args.timeout)
        shutil.rmtree(candidate / "correctness-cache", ignore_errors=True)
        deployed_pgo = {k: v for k, v in changed.items() if k != "GOFLAGS"}
        if "GOFLAGS" in overrides:
            deployed_pgo["GOFLAGS"] = overrides["GOFLAGS"]
        deployed_pgo["TGO_GO_TOOLEXEC"] = str(runtime / "go-tool")
        entry = make_launcher(candidate, candidate / "run", command, [], deployed_pgo, state["cwd"],
                              check_paths=[go, compiler, optimized, runtime / "go-tool"])
        built = {"schema": "tgo.build.v1", "strategy": "go-compiler-pgo", "source_run": str(capture),
                 "workload": {k: state[k] for k in ("command", "cwd", "env")}, "images": [],
                 "entry": entry, "entry_mode": "command", "state": "candidate",
                 "go_compiler": {"original": compiler, "optimized": str(optimized), "sha256": digest,
                                 "build": compilation, "fixture": fixture},
                 "profile_sha256": sha256(merged)}
        write_json(candidate / "build.json", built)
        screening = validate(candidate, args.search_pairs, args.timeout)
        built["validation"] = screening
        built["state"] = "validated" if screening["equivalent"] else "rejected"
        write_json(candidate / "build.json", built)
        result["candidates"].append({"name": "go-pgo", "validation": screening})
        selected = screening["equivalent"] and screening["min_reduction_percent"] > args.minimum_gain
        if selected:
            confirmation = validate(candidate, args.validate, args.timeout)
            result["confirmation"] = confirmation
            selected = confirmation["equivalent"] and confirmation["min_reduction_percent"] > args.minimum_gain
        if selected:
            result["selected"] = "go-pgo"
            deployed_env, checks = deployed_pgo, [go, compiler, optimized, runtime / "go-tool"]
        else:
            deployed_env, checks = overrides, []
        build_runtime(root)
        deployed = command[:1] if args.entry_mode == "executable" else command
        result["entry"] = make_launcher(root, args.entry or root / "run", deployed, [], deployed_env,
                                         None if args.entry_mode == "executable" else state["cwd"], check_paths=checks)
        result.update(entry_mode=args.entry_mode, optimized_compiler=str(optimized) if selected else None, state="complete")
    except BaseException as error:
        if 'built' in locals() and built.get("entry") and not built.get("validation", {}).get("equivalent"):
            Path(built["entry"]).unlink(missing_ok=True)
        result.update(state="failed", error=str(error)); raise
    finally:
        write_json(root / "tune.json", result)
    return result
