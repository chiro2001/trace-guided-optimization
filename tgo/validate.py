import hashlib
import os
from pathlib import Path
import statistics
import subprocess
import time
from .util import read_json, write_json, workload_env


def validate(bundle, pairs=5, timeout=300, metric=None):
    bundle = Path(bundle)
    build = read_json(bundle / "build.json")
    state = read_json(Path(build["source_run"]) / "run.json")
    trials = []
    expected = {r["optimized"] for r in build["images"] if r["input"]["interpreter"] is None}
    equivalent = True
    for i in range(pairs):
        pair = {}
        for arm in (["baseline", "candidate"] if i % 2 == 0 else ["candidate", "baseline"]):
            env = workload_env(); env.update(state["env"])
            prefix = bundle / f"validation-{i}-{arm}"
            for old in bundle.glob(prefix.name + ".*"):
                old.unlink()
            env["TGO_IMAGE_LOG"] = str(prefix)
            env["TGO_RESULT_FILE"] = str(prefix) + ".result.json"
            if metric:
                env["TGO_METRIC_FILE"] = str(prefix) + ".metrics"
                env["TGO_NATIVE_COUNTERS"] = "1"
            command = state["command"] if arm == "baseline" else [build["entry"]]
            t = time.perf_counter()
            p = subprocess.run(command, cwd=state["cwd"], env=env, capture_output=True, timeout=timeout)
            elapsed = time.perf_counter() - t
            stdout = prefix.with_suffix(".stdout"); stdout.write_bytes(p.stdout)
            stderr = prefix.with_suffix(".stderr"); stderr.write_bytes(p.stderr)
            observed = set()
            for f in bundle.glob(prefix.name + ".*.images"):
                for line in f.read_text().splitlines():
                    observed.add(line.split("\t", 2)[-1])
            measurement = elapsed
            pmu = []
            if metric:
                import json
                f = Path(str(prefix) + ".metrics")
                rows = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
                values = [r["nanoseconds"] for r in rows if r["region"] == metric]
                if not values:
                    raise ValueError(f"ROI metric missing: {metric}, {prefix}")
                measurement = statistics.median(values) / 1e9
                pmu = [r.get("pmu") for r in rows if r["region"] == metric]
            result_file = Path(env["TGO_RESULT_FILE"])
            correctness = p.stdout
            if result_file.exists():
                import json
                correctness = json.dumps(json.loads(result_file.read_text()), sort_keys=True, allow_nan=False).encode()
            pair[arm] = {"seconds": elapsed, "metric_seconds": measurement, "returncode": p.returncode,
                         "stdout_sha256": hashlib.sha256(p.stdout).hexdigest(),
                         "correctness_sha256": hashlib.sha256(correctness).hexdigest(),
                         "correctness_source": "result-json" if result_file.exists() else "stdout",
                         "pmu": pmu,
                         "loaded_optimized": sorted(observed & expected)}
            if p.returncode or (arm == "candidate" and not expected <= observed):
                equivalent = False
        if pair["baseline"]["correctness_sha256"] != pair["candidate"]["correctness_sha256"] or pair["baseline"]["correctness_source"] != pair["candidate"]["correctness_source"]:
            equivalent = False
        pair["reduction_percent"] = (1 - pair["candidate"]["metric_seconds"] / pair["baseline"]["metric_seconds"]) * 100
        trials.append(pair)
    reductions = [p["reduction_percent"] for p in trials]
    result = {"schema": "tgo.validation.v1", "pairs": trials, "equivalent": equivalent,
              "metric": metric or "whole-process-wall-time", "median_reduction_percent": statistics.median(reductions),
              "min_reduction_percent": min(reductions), "max_reduction_percent": max(reductions),
              "recommendation": "candidate" if equivalent and min(reductions) > 0 else "retain-baseline",
              "limits": "stdout equivalence and loaded-image checks; workload-specific correctness tests are additional"}
    write_json(bundle / "validation.json", result)
    return result
