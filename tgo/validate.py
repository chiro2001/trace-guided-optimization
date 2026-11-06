import hashlib
import os
from pathlib import Path
import statistics
import subprocess
import time
import math
import threading
import shutil
import uuid
from .util import read_json, write_json, workload_env, sha256, terminate_process_group,affinity_command
from .elf import is_executable_image


def execute_trial(command, prefix, env, cwd, timeout):
    command=affinity_command(command,env.get('TGO_WORKLOAD_CPUS'))
    stdout, stderr = Path(str(prefix) + ".stdout"), Path(str(prefix) + ".stderr")
    timed_out = False
    start = time.perf_counter()
    with stdout.open("wb") as out, stderr.open("wb") as err:
        p = subprocess.Popen(command, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
        finished = threading.Event()
        wait_errors = []
        def reap():
            try:
                p.wait()
            except BaseException as error:
                wait_errors.append(error)
            finally:
                finished.set()
        waiter = threading.Thread(target=reap, daemon=True)
        waiter.start()
        try:
            # Popen.wait(timeout) polls with exponential sleeps (up to 50ms),
            # which quantizes short whole-process benchmarks. Wait in a thread
            # using blocking waitpid and let Event enforce the deadline.
            if not finished.wait(timeout):
                raise subprocess.TimeoutExpired(command, timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as failure:
            timed_out = isinstance(failure, subprocess.TimeoutExpired)
            terminate_process_group(p)
            if not timed_out:
                raise
        waiter.join()
        if wait_errors:
            raise RuntimeError("could not reap validation workload") from wait_errors[0]
    return p.returncode, time.perf_counter() - start, timed_out, sha256(stdout)


def validate(bundle, pairs=5, timeout=300, metric=None):
    bundle = Path(bundle).resolve()
    # Every invocation keeps its own evidence, including probe, screening and
    # confirmation rounds. Never overwrite the earlier round's raw counters.
    evidence = bundle / "validations" / uuid.uuid4().hex
    evidence.mkdir(parents=True)
    build = read_json(bundle / "build.json")
    state = read_json(Path(build["source_run"]) / "run.json")
    trials = []
    warmup_trials = []
    warmup_count = state.get("validation", {}).get("warmup_pairs", 0)
    expected = {str(Path(r["optimized"]).resolve()) for r in build["images"]
                if not is_executable_image(r["input"]) or r['input'].get('interpreter')}
    equivalent = True
    for i in range(-warmup_count, pairs):
        pair = {}
        for arm in (["baseline", "candidate"] if i % 2 == 0 else ["candidate", "baseline"]):
            env = workload_env(); env.update(state["env"])
            support = bundle / "support"
            if support.is_dir():
                env["PYTHONPATH"] = str(support) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            if (bundle / "libtgo_roi.so").exists():
                env.setdefault("TGO_ROI_LIBRARY", str(bundle / "libtgo_roi.so"))
            label = f"warmup-{i + warmup_count}" if i < 0 else f"validation-{i}"
            prefix = evidence / f"{label}-{arm}"
            env["TGO_IMAGE_LOG"] = str(prefix)
            env["TGO_RESULT_FILE"] = str(prefix) + ".result.json"
            go_cache = None
            if state.get("validation", {}).get("go_cache") == "cold":
                go_cache = Path(str(prefix) + ".cache")
                go_cache.mkdir()
                env["GOCACHE"] = str(go_cache)
                env["TGO_VALIDATION_GOCACHE"] = str(go_cache)
            if build.get("go_compiler"):
                env["TGO_GO_EXEC_LOG"] = str(prefix) + ".tools"
            if metric:
                env["TGO_METRIC_FILE"] = str(prefix) + ".metrics"
                env["TGO_NATIVE_COUNTERS"] = "1"
            tail = state["command"][1:] if build.get("entry_mode") == "executable" else []
            command = state["command"] if arm == "baseline" else [build["entry"], *tail]
            baseline_times = [p["baseline"]["seconds"] for p in [*warmup_trials, *trials]]
            if "baseline" in pair:
                baseline_times.append(pair["baseline"]["seconds"])
            limit = min(timeout, max(5, statistics.median(baseline_times) * 3 + 2)) if arm == "candidate" and baseline_times else timeout
            try:
                code, elapsed, timed_out, stdout_sha = execute_trial(command, prefix, env, state["cwd"], limit)
            finally:
                if go_cache:
                    shutil.rmtree(go_cache, ignore_errors=True)
            observed = set()
            for f in evidence.glob(prefix.name + ".*.images"):
                for line in f.read_text().splitlines():
                    observed.add(line.split("\t", 2)[-1])
            measurement = elapsed
            pmu = []
            missing_metric = False
            if metric:
                import json
                f = Path(str(prefix) + ".metrics")
                rows = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
                values = [r["nanoseconds"] for r in rows if r["region"] == metric]
                if not values:
                    missing_metric = True
                elif not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in values):
                    raise ValueError(f"invalid ROI metric: {prefix}")
                else:
                    measurement = statistics.median(values) / 1e9
                pmu = [r.get("pmu") for r in rows if r["region"] == metric]
            result_file = Path(env["TGO_RESULT_FILE"])
            executed_compilers = []
            if build.get("go_compiler"):
                tool_log = Path(env["TGO_GO_EXEC_LOG"])
                if tool_log.exists():
                    executed_compilers = [line.split("\t")[2] for line in tool_log.read_text().splitlines() if len(line.split("\t")) == 3]
                if arm == "candidate" and build["go_compiler"]["optimized"] not in executed_compilers:
                    equivalent = False
            correctness_sha = stdout_sha
            if result_file.exists():
                import json
                correctness_sha = hashlib.sha256(json.dumps(json.loads(result_file.read_text()), sort_keys=True, allow_nan=False).encode()).hexdigest()
            pair[arm] = {"seconds": elapsed, "metric_seconds": measurement, "returncode": code,
                         "evidence_prefix": str(prefix),
                         "timed_out": timed_out, "metric_missing": missing_metric,
                         "stdout_sha256": stdout_sha,
                         "correctness_sha256": correctness_sha,
                         "correctness_source": "result-json" if result_file.exists() else "stdout",
                         "pmu": pmu,
                         "loaded_optimized": sorted(observed & expected)}
            if build.get("go_compiler"):
                pair[arm]["executed_compilers"] = sorted(set(executed_compilers))
            if code or timed_out or missing_metric or (arm == "candidate" and not expected <= observed):
                equivalent = False
        if pair["baseline"]["correctness_sha256"] != pair["candidate"]["correctness_sha256"] or pair["baseline"]["correctness_source"] != pair["candidate"]["correctness_source"]:
            equivalent = False
        pair["reduction_percent"] = (1 - pair["candidate"]["metric_seconds"] / pair["baseline"]["metric_seconds"]) * 100
        if i < 0:
            warmup_trials.append(pair)
        else:
            trials.append(pair)
        if not equivalent:
            break
    reductions = [p["reduction_percent"] for p in trials] or [0]
    result = {"schema": "tgo.validation.v1", "pairs": trials, "equivalent": equivalent,
              "evidence_directory": str(evidence),
              "requested_pairs": pairs, "completed_pairs": len(trials),
              "warmup_pairs": warmup_trials,
              "metric": metric or "whole-process-wall-time", "median_reduction_percent": statistics.median(reductions),
              "min_reduction_percent": min(reductions), "max_reduction_percent": max(reductions),
              "recommendation": "candidate" if equivalent and min(reductions) > 0 else "retain-baseline",
              "limits": "stdout equivalence and loaded-image checks; workload-specific correctness tests are additional"}
    write_json(evidence / "validation.json", result)
    write_json(bundle / "validation.json", result)
    return result
