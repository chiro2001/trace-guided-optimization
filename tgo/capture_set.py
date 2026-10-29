"""Immutable references to compatible, independently normalized captures."""
from copy import deepcopy
from pathlib import Path
from .util import read_json, write_json, sha256, IdentityError

SCHEMA = "tgo.profile-set.v1"


def fingerprint(state):
    backend = state["backend"]
    if state.get("sampling"):
        return state["sampling"]
    command = state.get("collector_command") or []
    # Collector options end before the workload's argv. Its own -c/-e/-F
    # arguments must never become the sampling contract.
    command = command[:command.index("--")] if "--" in command else command
    def option(name, default=None):
        return command[command.index(name)+1] if name in command else default
    period = option("-c", "4096" if backend == "spe" else None)
    return {"backend": backend, "event": option("-e", "cycles:u" if backend == "perf" else None),
        "period": int(period) if period else None,
        "frequency": int(option("-F", "999")) if backend == "perf" and not period else None,
        "branch_stack": bool(state.get("branch_stack", False))}


def workload_contract(state):
    command = state.get("user_command") or state.get("training_command") or state["command"]
    actual = state["command"]
    hook = actual[3] if len(actual)>4 and Path(actual[1]).name == "python_entry.py" and actual[2] == "--hook" else None
    roi = state.get("roi", "whole")
    window = None
    if roi == "window":
        collector = state.get("collector_command") or []
        collector = collector[:collector.index("--")] if "--" in collector else collector
        # Compare effective millisecond bounds, including older captures.
        window = state.get("roi_window") or next((item.split("=", 1)[1] for item in collector if item.startswith("--delay=")), None)
        if window is None:
            raise ValueError("time-window capture lacks recorded ROI bounds")
    return {"command": command, "cwd": state["cwd"], "env": state["env"], "roi": roi, "roi_window": window,
            "python_hook": hook}


def check_file(path, expected):
    try:
        digest = sha256(path)
    except OSError as error:
        raise IdentityError(f"capture evidence is no longer readable: {path}") from error
    if digest != expected:
        raise IdentityError(f"capture evidence changed: {path}")


def verified_sources(profile):
    sources = []
    for ref in profile.get("sources", []):
        root = Path(ref["run"])
        check_file(root / "run.json", ref["run_sha256"])
        check_file(root / "profile.json", ref["profile_sha256"])
        state, original = read_json(root / "run.json"), read_json(root / "profile.json")
        if state["state"] != "recorded":
            raise ValueError(f"capture is no longer complete: {root}")
        if original.get("schema") == SCHEMA:
            raise ValueError("capture-set sources must be flattened leaves")
        if original.get("raw_data"):
            check_file(original["raw_data"]["path"], original["raw_data"]["sha256"])
        for capture in original.get("captures", []):
            check_file(capture["trace"], capture["trace_sha256"])
            check_file(capture["maps"], capture["maps_sha256"])
        for row in original["images"]:
            if row.get("preaggregate_sha256"):
                check_file(row["preaggregate"], row["preaggregate_sha256"])
        sources.append((state, original))
    return sources


def observations(profile, row):
    if profile.get("schema") != SCHEMA:
        return [row]
    result = []
    for observation in row["observations"]:
        ref = profile["sources"][observation["source"]]
        check_file(Path(ref["run"]) / "profile.json", ref["profile_sha256"])
        original = read_json(Path(ref["run"]) / "profile.json")
        matches = [item for item in original["images"] if item["path"] == row["path"] and item["sha256"] == row["sha256"]]
        if len(matches) != 1:
            raise ValueError(f"capture observation does not uniquely identify {row['path']}")
        result.append(matches[0])
    return result


def merge_captures(runs, output, allow_variation=False):
    roots, seen, digests, active = [], set(), set(), set()
    def flatten(path):
        root = Path(path).resolve()
        if root in active:
            raise ValueError("cyclic capture set")
        active.add(root)
        state, profile = read_json(root / "run.json"), read_json(root / "profile.json")
        if state["state"] != "recorded" or state["backend"] not in ("perf", "spe", "qemu"):
            raise ValueError("merge requires completed perf/SPE/QEMU captures; Go CPU profiles use the Go strategy")
        if profile.get("schema") == SCHEMA:
            verified_sources(profile)
            for ref in profile["sources"]:
                flatten(ref["run"])
        else:
            digest = sha256(root / "run.json")
            if root in seen or digest in digests:
                raise ValueError(f"duplicate capture: {root}")
            seen.add(root); digests.add(digest); roots.append(root)
        active.remove(root)
    for root in runs:
        flatten(root)
    if not roots:
        raise ValueError("provide at least one completed capture")
    refs = [{"run": str(root), "run_sha256": sha256(root / "run.json"),
             "profile_sha256": sha256(root / "profile.json")} for root in roots]
    source_profiles = verified_sources({"sources": refs})
    base_state, base_profile = source_profiles[0]
    sampler, contract = fingerprint(base_state), workload_contract(base_state)
    variations, images, weights = [], {}, {}
    for index, (state, profile) in enumerate(source_profiles):
        if profile["backend"] != base_profile["backend"] or fingerprint(state) != sampler:
            raise ValueError("captures have incompatible backends, sampling events, periods or branch-stack modes")
        current = workload_contract(state)
        if current != contract:
            if not allow_variation:
                raise ValueError("capture workload/ROI differs; use --allow-workload-variation for deliberate training variants")
            variations.append({"source": index, "different_fields": [key for key in contract if current[key] != contract[key]]})
        for row in profile["images"]:
            path = row["path"]
            if path in images and images[path]["sha256"] != row["sha256"]:
                raise ValueError(f"ELF identity differs across captures: {path}")
            if path not in images:
                images[path] = {key: value for key, value in row.items()
                    if key not in ("perf_data", "preaggregate", "preaggregate_sha256", "observations")}
                images[path]["observations"] = []
                weights[path] = []
            weight = row.get("dynamic_instructions", row.get("sample_percent", 0))
            weights[path].append(weight)
            if weight > 0:
                images[path]["observations"].append({"source": index})
    qemu = base_profile["backend"] == "qemu"
    for path, row in images.items():
        row["dynamic_instructions" if qemu else "sample_percent"] = sum(weights[path]) / (1 if qemu else len(roots))
    rows = sorted(images.values(), key=lambda row: -row.get("dynamic_instructions", row.get("sample_percent", 0)))
    merged = {"schema": SCHEMA, "backend": base_profile["backend"], "sources": refs, "images": rows,
        "quality": {"kind": "independently-normalized-captures", "runs": len(roots),
                    "hotspot_weight": "summed dynamic instruction counts" if qemu else "mean per-run sample share",
                    "fdata_weight": "sum of counts after independent ELF mapping", "workload_variations": variations}}
    if qemu:
        merged.update(instructions=sum(p["instructions"] for _, p in source_profiles),
            unmapped_instructions=sum(p.get("unmapped_instructions", 0) for _, p in source_profiles),
            marker_windows=sum(p.get("marker_windows", 0) for _, p in source_profiles),
            truncated=any(p.get("truncated", False) for _, p in source_profiles))
    out = Path(output).resolve(); out.mkdir(parents=True, exist_ok=False)
    state = deepcopy(base_state)
    state.update(aggregate=True, sampling=sampler, state="recorded", phase="complete", source_runs=len(roots),
                 collector_command=None, training_contract=contract, pid=None)
    state.pop("collector_sha256", None)
    starts = [s["started_unix"] for s, _ in source_profiles if "started_unix" in s]
    finishes = [s["finished_unix"] for s, _ in source_profiles if "finished_unix" in s]
    if starts:
        state["started_unix"] = min(starts)
    if finishes:
        state["finished_unix"] = max(finishes)
    write_json(out / "run.json", state)
    write_json(out / "profile.json", merged)
    return merged
