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
from dataclasses import dataclass, replace
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


def blend_profiles(go, target, default, root, env, timeout, target_fraction=0.25):
    """Weight pprof samples through the public tool, preserving function metadata."""
    totals = []
    for label, path in [("target", target), ("default", default)]:
        report = go_command(["tool", "pprof", "-raw", "-symbolize=none", path],
                            go, env, root, "hybrid-" + label + "-info", timeout)
        totals.append(profile_totals(Path(report["stdout"]).read_text()))
    ratio = totals[1]["samples"] * target_fraction / ((1-target_fraction) * totals[0]["samples"])
    target_scale, default_scale = (ratio, 1.0) if ratio >= 1 else (1.0, 1/ratio)
    inputs = [target, default]
    scaled = root / "hybrid-scaled.pprof"
    which = 0 if ratio >= 1 else 1
    multiplier = target_scale if which == 0 else default_scale
    scaling = go_command(["tool", "pprof", "-proto", "-symbolize=none", "-divide_by=" + str(1/multiplier),
                          "-output", scaled, inputs[which]], go, env, root, "hybrid-scale", timeout)
    inputs[which] = scaled
    output = root / "hybrid.pprof"
    merging = go_command(["tool", "pprof", "-proto", "-symbolize=none", "-output", output, *inputs],
                         go, env, root, "hybrid-merge", timeout)
    check = go_command(["tool", "preprofile", "-i", output, "-o", root / "hybrid-check.preprocessed"],
                       go, env, root, "hybrid-check", timeout)
    return output, {"format": "pprof", "requested_target_sample_fraction": target_fraction,
        "target_scale": target_scale, "default_scale": default_scale, "input_totals": totals,
        "sources": [{"path": str(p), "sha256": sha256(p)} for p in (target, default)],
        "sha256": sha256(output), "scaling": scaling, "merging": merging, "metadata_validation": check}


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


def build_settings(go, compiler, env):
    result = subprocess.run([go, "version", "-m", "-json", compiler], env=env, capture_output=True, text=True)
    if result.returncode == 0:
        return {row["Key"]: row["Value"] for row in json.loads(result.stdout).get("Settings", [])}
    text = subprocess.check_output([go, "version", "-m", compiler], env=env, text=True)
    settings = {}
    for line in text.splitlines():
        if line.startswith("\tbuild\t"):
            key, _, value = line[7:].partition("=")
            settings[key] = value.strip('"')
    return settings


@dataclass
class CompilerContext:
    go: str
    compiler: str
    version: str
    info: dict
    base: dict
    overrides: dict
    runtime: Path
    capture: Path
    profile: Path
    state: dict
    settings: dict
    timeout: float


def build_candidate(ctx, candidate, extra_gc="", extra_ld=""):
    candidate.mkdir()
    build_runtime(candidate)
    optimized = candidate / "compile"
    env = dict(ctx.base)
    env.update(GOFLAGS="", GOOS=ctx.info["GOHOSTOS"], GOARCH=ctx.info["GOHOSTARCH"], GOTOOLCHAIN="local")
    for key in ("CGO_ENABLED", "GOAMD64", "GOARM64", "GOARM", "GOMIPS", "GOMIPS64", "GOEXPERIMENT"):
        if key in ctx.settings:
            env[key] = ctx.settings[key]
    env.pop("TGO_DISABLE", None)
    flags = []
    if ctx.settings.get("-trimpath") == "true":
        flags.append("-trimpath")
    gc = ctx.settings.get("-gcflags", "")
    if extra_gc:
        gc = gc + " " + extra_gc if gc else "cmd/...=" + extra_gc
    if gc:
        flags.append("-gcflags=" + gc.strip())
    ld = (ctx.settings.get("-ldflags", "") + " " + extra_ld).strip()
    if ld:
        flags.append("-ldflags=" + ld)
    compilation = go_command(["build", "-pgo=" + str(ctx.profile), *flags, "-o", optimized, "cmd/compile"],
                             ctx.go, env, candidate, "compiler-build", ctx.timeout)
    actual_version = subprocess.check_output([optimized, "-V=full"], env=ctx.base, text=True).strip()
    if actual_version != ctx.version:
        raise ValueError(f"rebuilt compiler version differs: {actual_version!r} != {ctx.version!r}")
    digest = sha256(optimized)
    pgo_base = dict(ctx.overrides); pgo_base["GOFLAGS"] = ctx.base.get("GOFLAGS", "")
    changed = adapter_env(pgo_base, ctx.runtime, ctx.compiler, ctx.version, digest, optimized)
    checkenv = dict(ctx.base); checkenv.update(changed)
    checkenv.update(GOOS=ctx.info["GOHOSTOS"], GOARCH=ctx.info["GOHOSTARCH"],
                    GOCACHE=str(candidate / "correctness-cache"), TGO_GO_EXEC_LOG=str(candidate / "correctness-tools.tsv"))
    fixture = correctness_fixture(ctx.go, checkenv, candidate, ctx.timeout)
    if str(optimized) not in (candidate / "correctness-tools.tsv").read_text():
        raise ValueError("correctness fixture did not execute the candidate compiler")
    shutil.rmtree(candidate / "correctness-cache", ignore_errors=True)
    deployed = {k: v for k, v in changed.items() if k != "GOFLAGS"}
    if "GOFLAGS" in ctx.overrides:
        deployed["GOFLAGS"] = ctx.overrides["GOFLAGS"]
    deployed["TGO_GO_TOOLEXEC"] = str(ctx.runtime / "go-tool")
    checks = [ctx.go, ctx.compiler, optimized, ctx.runtime / "go-tool"]
    entry = make_launcher(candidate, candidate / "run", ctx.state["command"], [], deployed, ctx.state["cwd"], check_paths=checks)
    built = {"schema": "tgo.build.v1", "strategy": "go-compiler-pgo", "source_run": str(ctx.capture),
             "workload": {k: ctx.state[k] for k in ("command", "cwd", "env")}, "images": [],
             "entry": entry, "entry_mode": "command", "state": "candidate",
             "go_compiler": {"original": ctx.compiler, "optimized": str(optimized), "sha256": digest,
                             "build": compilation, "fixture": fixture, "build_settings": build_settings(ctx.go, optimized, ctx.base)},
             "profile_sha256": sha256(ctx.profile)}
    write_json(candidate / "build.json", built)
    return built, deployed, checks


def tune_go(args, root_exists=False):
    root = Path(args.output).absolute()
    root.mkdir(parents=True, exist_ok=root_exists)
    if args.pid or args.roi != "whole" or args.python_hook:
        raise ValueError("Go compiler PGO records compiler-process CPU profiles; PID/time/marker ROI requires a sampling strategy")
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
                 "validation": {"go_cache": args.go_cache, "warmup_pairs": args.go_warmups}, "toolchain": info}
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
        settings = build_settings(go, compiler, base)
        result["compiler"]["build_settings"] = settings
        ctx = CompilerContext(go, compiler, version, info, base, overrides, runtime, capture,
                              merged, state, settings, args.timeout)
        choices = {"pgo": ("", ""), "compact": ("-d=pgoinlinebudget=256", ""),
                   "aligned": ("", "-funcalign=" + ("128" if info["GOHOSTARCH"] == "arm64" else "64"))}
        default_profile = goroot / "src/cmd/compile/default.pgo"
        if default_profile.exists():
            choices["hybrid"] = ("", "")
        best = None
        for name in args.go_variant or choices:
            candidate = root / name
            try:
                if name not in choices:
                    raise ValueError(f"Go toolchain does not provide the profile required by variant {name}")
                variant_ctx = ctx
                if name == "hybrid":
                    blended, blend = blend_profiles(go, merged, default_profile, capture, base, args.timeout)
                    write_json(capture / "hybrid.json", blend)
                    variant_ctx = replace(ctx, profile=blended)
                built, candidate_env, checks = build_candidate(variant_ctx, candidate, *choices[name])
                screening = validate(candidate, args.search_pairs, args.timeout, args.metric)
                built["validation"] = screening
                built["state"] = "validated" if screening["equivalent"] else "rejected"
                write_json(candidate / "build.json", built)
                result["candidates"].append({"name": name, "validation": screening})
                if screening["equivalent"] and screening["min_reduction_percent"] > args.minimum_gain:
                    if best is None or screening["median_reduction_percent"] > best["gain"]:
                        best = {"name": name, "path": candidate, "env": candidate_env, "checks": checks,
                                "compiler": built["go_compiler"]["optimized"], "gain": screening["median_reduction_percent"]}
                if not screening["equivalent"]:
                    Path(built["entry"]).unlink(missing_ok=True)
            except (ValueError, RuntimeError, OSError) as error:
                result["candidates"].append({"name": name, "error": str(error)})
                failed = candidate / "build.json"
                if failed.exists():
                    record = read_json(failed)
                    Path(record["entry"]).unlink(missing_ok=True)
                    record.update(state="failed", error=str(error)); write_json(failed, record)
            write_json(root / "tune.json", result)
        selected = False
        if best:
            confirmation = validate(best["path"], args.validate, args.timeout, args.metric)
            result["confirmation"] = confirmation
            selected = confirmation["equivalent"] and confirmation["min_reduction_percent"] > args.minimum_gain
            record = read_json(best["path"] / "build.json"); record["validation"] = confirmation
            write_json(best["path"] / "build.json", record)
        if selected:
            result["selected"] = best["name"]
            deployed_env, checks = best["env"], best["checks"]
        else:
            deployed_env, checks = overrides, []
        build_runtime(root)
        deployed = command[:1] if args.entry_mode == "executable" else command
        result["entry"] = make_launcher(root, args.entry or root / "run", deployed, [], deployed_env,
                                         None if args.entry_mode == "executable" else state["cwd"], check_paths=checks)
        result.update(entry_mode=args.entry_mode, optimized_compiler=best["compiler"] if selected else None, state="complete")
        final_build = {"schema": "tgo.build.v1", "strategy": "go-compiler-pgo", "source_run": str(capture),
            "images": [], "entry": result["entry"], "entry_mode": args.entry_mode,
            "state": "validated" if selected else "baseline", "validation": result.get("confirmation"),
            "workload": {k: state[k] for k in ("command", "cwd", "env")}}
        if selected:
            final_build["go_compiler"] = read_json(best["path"] / "build.json")["go_compiler"]
        write_json(root / "build.json", final_build)
    except BaseException as error:
        if 'built' in locals() and built.get("entry") and not built.get("validation", {}).get("equivalent"):
            Path(built["entry"]).unlink(missing_ok=True)
        result.update(state="failed", error=str(error)); raise
    finally:
        write_json(root / "tune.json", result)
    return result
