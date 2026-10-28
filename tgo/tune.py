from copy import deepcopy
from pathlib import Path
from .collect import record
from .optimize import optimize
from .runtime import build_runtime, make_launcher
from .util import read_json, write_json
from .validate import validate


def tune(args):
    root = Path(args.output).absolute()
    root.mkdir(parents=True, exist_ok=False)
    capture = Path(args.run).resolve() if args.run else root / "capture"
    if not args.run:
        recording = deepcopy(args); recording.output = str(capture)
        record(recording)
    state = read_json(capture / "run.json")
    variants = [
        ("ext-tsp", []),
        ("compact", ["-split-functions=false", "-split-all-cold=false"]),
        ("aligned", ["-align-functions=128", "-align-functions-max-bytes=16"]),
    ]
    result = {"schema": "tgo.tune.v1", "capture": str(capture), "candidates": [], "selected": "baseline"}
    best, gain = None, args.minimum_gain
    for name, flags in variants:
        candidate = deepcopy(args)
        candidate.run = str(capture); candidate.output = str(root / name)
        candidate.entry = None; candidate.bolt_arg = flags + args.bolt_arg
        candidate.validate = args.search_pairs
        try:
            built = optimize(candidate)
            v = built["validation"]
            result["candidates"].append({"name": name, "validation": v})
            if v["equivalent"] and v["min_reduction_percent"] > args.minimum_gain and v["median_reduction_percent"] > gain:
                best, gain = root / name, v["median_reduction_percent"]
        except (RuntimeError, ValueError, OSError) as e:
            result["candidates"].append({"name": name, "error": str(e)})
        write_json(root / "tune.json", result)
    images = []
    if best:
        # Fresh processes after selecting a layout; screening scores are not final evidence.
        confirmation = validate(best, args.validate, args.timeout, args.metric)
        result["confirmation"] = confirmation
        if confirmation["equivalent"] and confirmation["min_reduction_percent"] > args.minimum_gain:
            result["selected"] = best.name
            chosen = read_json(best / "build.json")
            chosen["validation"] = confirmation
            write_json(best / "build.json", chosen)
            images = chosen["images"]
    build_runtime(root)
    result["entry"] = make_launcher(root, args.entry or root / "run", state["command"], images, state["env"], state["cwd"])
    result["optimized_images"] = [r["optimized"] for r in images]
    write_json(root / "tune.json", result)
    return result
