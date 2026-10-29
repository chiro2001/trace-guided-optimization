from copy import deepcopy
from pathlib import Path
import hashlib
import itertools
from .collect import record
from .optimize import optimize, verify_images, IdentityError
from .runtime import build_runtime, make_launcher
from .util import read_json, write_json, sha256
from .validate import validate
from .selection import screening_score, confirmed
from .capture_set import merge_captures, fingerprint
from .recipes import resolve_recipe
from .util import tool

LAYOUTS = {
    "ext-tsp": [],
    "compact": ["-split-functions=false", "-split-all-cold=false"],
    "aligned": ["-align-functions=128", "-align-functions-max-bytes=16"],
}


def image_subsets(images, exhaustive=False):
    count = len(images)
    if exhaustive:
        if count > 4:
            raise ValueError("--image-search all supports at most four images; reduce --top")
        indices = [group for size in range(count-1, 0, -1) for group in itertools.combinations(range(count), size)]
    else:
        # Linear fallback for large sets: drop one suspect, then try singletons.
        indices = [tuple(j for j in range(count) if j != i) for i in range(count)]
        indices += [(i,) for i in range(count)]
    seen = set()
    for group in indices:
        if group and len(group) < count and group not in seen:
            seen.add(group)
            yield [images[i] for i in group]


def compose_bundle(path, source_build, images):
    """Reuse rewritten objects; subset search never reruns the rewriter."""
    verify_images(images)
    path.mkdir()
    build_runtime(path)
    state = read_json(Path(source_build["source_run"]) / "run.json")
    entry = make_launcher(path, path / "run", state["command"], images, state["env"], state["cwd"])
    built = {"schema": "tgo.build.v1", "strategy": "bolt-subset", "source_run": source_build["source_run"],
        "profile_sha256": source_build["profile_sha256"], "images": images, "entry": entry,
        "entry_mode": "command", "state": "candidate", "workload": source_build["workload"],
        "reused_objects": True}
    write_json(path / "build.json", built)
    return built


def evaluate(path, pairs, args):
    built = read_json(path / "build.json")
    verify_images(built["images"])
    result = validate(path, pairs, args.timeout, args.metric)
    verify_images(built["images"])
    built.setdefault("validation_history", []).append(result)
    built["validation"] = result
    built["state"] = "validated" if result["equivalent"] else "rejected"
    if not result["equivalent"]:
        Path(built["entry"]).unlink(missing_ok=True)
    write_json(path / "build.json", built)
    return result


def discover_go(args, root, capture, state, profile):
    compilers = [row for row in profile["images"] if row.get("go") and Path(row["path"]).name == "compile"]
    if not compilers:
        return None
    from .gopgo import tune_go
    compiler = max(compilers, key=lambda row: row.get("sample_percent", row.get("dynamic_instructions", 0)))
    adapter = deepcopy(args)
    adapter.run = None; adapter.output = str(root / "go-compiler")
    adapter.command = state["command"]
    adapter.env = [f"{k}={v}" for k, v in state["env"].items()]
    adapter.entry = args.entry or str(root / "run")
    inferred = Path(compiler["path"]).parents[3] / "bin/go"
    adapter.go = args.go or (str(inferred) if inferred.is_file() else None)
    result = tune_go(adapter)
    result["discovery"] = {"capture": str(capture), "compiler": compiler["path"]}
    write_json(root / "build.json", read_json(Path(adapter.output) / "build.json"))
    write_json(root / "tune.json", result)
    return result


def discover_cpython(args, root, capture, state, profile):
    if state.get("training_images"):
        return None
    explicit = args.strategy == "cpython"
    if not explicit and (not resolve_recipe(args, state, profile) or not tool("gcc", required=False)):
        return None
    if args.pid or state.get("roi") != "marker":
        if explicit:
            raise ValueError("CPython compiler training currently requires launch mode and an explicit marker ROI")
        return None
    from .cpython import tune_cpython
    adapter = deepcopy(args)
    adapter.roi = state["roi"]
    adapter.python_hook = None
    adapter.output = str(root / "cpython")
    adapter.entry = args.entry or str(root / "run")
    result = tune_cpython(adapter, capture)
    result["discovery"] = {"capture": str(capture), "adapter": "CPython ROI PGO/LTO"}
    build_runtime(root)
    write_json(root / "build.json", read_json(Path(adapter.output) / "build.json"))
    return result


def tune(args):
    root = Path(args.output).absolute(); root.mkdir(parents=True, exist_ok=False)
    capture = Path(args.run).resolve() if args.run else root / "capture"
    result = {"schema": "tgo.tune.v1", "capture": str(capture), "candidates": [], "probes": [],
              "confirmations": [], "selected": "baseline", "state": "recording"}
    write_json(root / "tune.json", result)
    try:
        if not args.run:
            repetitions = getattr(args, "train_runs", 1)
            first = capture if repetitions == 1 else root / "training/capture-000"
            recording = deepcopy(args); recording.output = str(first)
            record(recording)
            first_state, first_profile = read_json(first / "run.json"), read_json(first / "profile.json")
            if args.strategy == "auto":
                discovery = discover_go(args, root, first, first_state, first_profile)
                if discovery:
                    result = discovery
                    return result
            if args.strategy in ("auto", "cpython"):
                discovery = discover_cpython(args, root, first, first_state, first_profile)
                if discovery:
                    result = discovery
                    return result
            captures = [first]
            result["training_runs_completed"] = 1
            write_json(root / "tune.json", result)
            sampler = fingerprint(first_state)
            for index in range(1, repetitions):
                recording = deepcopy(args)
                recording.output = str(root / f"training/capture-{index:03d}")
                recording.backend = first_state["backend"]
                recording.event = sampler["event"]
                record(recording)
                captures.append(Path(recording.output))
                result["training_runs_completed"] = len(captures)
                write_json(root / "tune.json", result)
            if repetitions > 1:
                merge_captures(captures, capture)
            result["training_runs_completed"] = repetitions
        state = read_json(capture / "run.json")
        profile = read_json(capture / "profile.json")
        result["training_runs_completed"] = state.get("source_runs", 1)
        if args.strategy == "auto":
            discovery = discover_go(args, root, capture, state, profile)
            if discovery:
                result = discovery
                return result
        if args.strategy in ("auto", "cpython"):
            discovery = discover_cpython(args, root, capture, state, profile)
            if discovery:
                result = discovery
                return result
        result.update(state="screening", automatic_images=not bool(args.image), bolt_policy=args.bolt_mode,
                      image_search=args.image_search)
        write_json(root / "tune.json", result)
        pool = []
        for name in args.layout or LAYOUTS:
            base = root / name
            candidate = deepcopy(args)
            candidate.run = str(capture); candidate.output = str(base)
            candidate.entry = None; candidate.bolt_arg = LAYOUTS[name] + args.bolt_arg
            candidate.entry_mode = "command"; candidate.validate = 0
            try:
                built = optimize(candidate)
                screening = evaluate(base, args.search_pairs, args)
                item = {"name": name, "directory": str(base), "images": [r["original"] for r in built["images"]],
                        "failed_images": built["failed_images"], "validation": screening}
                result["candidates"].append(item)
                score = screening_score(screening, args.minimum_gain)
                if score is not None:
                    pool.append((score, name, base))
                write_json(root / "tune.json", result)
                search_subsets = args.image_search == "all" or (args.image_search == "adaptive" and score is None)
                if search_subsets:
                    promising = []
                    for images in image_subsets(built["images"], args.image_search == "all"):
                        identity = hashlib.sha256('\n'.join(r["original"] for r in images).encode()).hexdigest()[:10]
                        subset_name = name + "-subset-" + identity
                        path = root / subset_name
                        compose_bundle(path, built, images)
                        probe = evaluate(path, 1, args)
                        result["probes"].append({"name": subset_name, "directory": str(path),
                            "images": [r["original"] for r in images], "validation": probe})
                        probe_score = screening_score(probe, args.minimum_gain)
                        if probe_score is not None:
                            promising.append((probe_score, subset_name, path))
                        write_json(root / "tune.json", result)
                    # Quick subset probes only nominate candidates. Run a full
                    # screening round before comparing them with joint layouts.
                    for _, subset_name, path in sorted(promising, reverse=True)[:2]:
                        screening = evaluate(path, args.search_pairs, args)
                        subset = read_json(path / "build.json")
                        result["candidates"].append({"name": subset_name, "directory": str(path),
                            "images": [r["original"] for r in subset["images"]], "validation": screening})
                        score = screening_score(screening, args.minimum_gain)
                        if score is not None:
                            pool.append((score, subset_name, path))
            except IdentityError:
                raise
            except (RuntimeError, ValueError, OSError) as error:
                result["candidates"].append({"name": name, "error": str(error)})
            write_json(root / "tune.json", result)
        images = []
        result["state"] = "confirming"; write_json(root / "tune.json", result)
        for _, name, path in sorted(pool, reverse=True)[:args.confirm_candidates]:
            confirmation = evaluate(path, args.validate, args)
            result["confirmations"].append({"name": name, "validation": confirmation})
            result["confirmation"] = confirmation
            write_json(root / "tune.json", result)
            if confirmed(confirmation, args.minimum_gain):
                result["selected"] = name
                chosen = read_json(path / "build.json")
                images = chosen["images"]
                break
        verify_images(images)
        build_runtime(root)
        result["entry_mode"] = args.entry_mode
        deployed = state["command"][:1] if args.entry_mode == "executable" else state["command"]
        result["entry"] = make_launcher(root, args.entry or root / "run", deployed, images, state["env"],
                                         None if args.entry_mode == "executable" else state["cwd"])
        result["optimized_images"] = [r["optimized"] for r in images]
        result["state"] = "complete"
        write_json(root / "build.json", {"schema": "tgo.build.v1", "strategy": "bolt", "source_run": str(capture),
            "images": images, "entry": result["entry"], "entry_mode": args.entry_mode,
            "state": "validated" if images else "baseline", "validation": result.get("confirmation"),
            "workload": {k: state[k] for k in ("command", "cwd", "env")}})
    except BaseException as error:
        result.update(state="failed", error=str(error)); raise
    finally:
        write_json(root / "tune.json", result)
    return result
