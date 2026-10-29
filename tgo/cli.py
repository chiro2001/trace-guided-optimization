import argparse
import json
import os
from pathlib import Path
import sys
import time
from . import __version__
from .collect import doctor, record
from .optimize import optimize
from .util import read_json, write_json, command_path


def main(argv=None):
    p = argparse.ArgumentParser(prog="tgo", description="ROI-driven native code layout optimization")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="probe tools and PMU access")
    r = sub.add_parser("record", help="record a workload or attach to a PID")
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--backend", choices=["auto", "perf", "spe", "qemu"], default="auto")
    r.add_argument("--roi", choices=["whole", "marker", "window"], default="whole")
    r.add_argument("--pid", type=int)
    r.add_argument("--duration", type=float)
    r.add_argument("--delay", type=float, default=0)
    r.add_argument("--frequency", type=int, default=999)
    r.add_argument("--period", type=int, help="sample period; SPE defaults to 4096")
    r.add_argument("--event")
    r.add_argument("--branch-stack", action="store_true")
    r.add_argument("--python-hook", metavar="MODULE:CLASS.METHOD")
    r.add_argument("--qemu", default=os.environ.get("TGO_QEMU"))
    r.add_argument("--perf", default=os.environ.get("TGO_PERF"))
    cached_header = Path(__file__).resolve().parents[1] / "deps/install/include/qemu-plugin.h"
    r.add_argument("--qemu-header", default=os.environ.get("TGO_QEMU_HEADER") or (str(cached_header) if cached_header.exists() else None))
    r.add_argument("--max-insns", type=int, default=1000000000)
    r.add_argument("--allow-truncated", action="store_true")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("command", nargs=argparse.REMAINDER)
    o = sub.add_parser("optimize", help="build an immutable DSO bundle and native ELF entry")
    o.add_argument("--run", required=True)
    o.add_argument("-o", "--output", required=True)
    o.add_argument("--entry", help="new ELF path, e.g. /path/to/python3.tgo; never overwrites")
    o.add_argument("--entry-mode", choices=["command", "executable"], default="command")
    o.add_argument("--image", action="append", default=[])
    o.add_argument("--top", type=int, default=3)
    o.add_argument("--recipe", choices=["auto", "standard", "eager-v12"], default="auto")
    o.add_argument("--bolt")
    o.add_argument("--perf2bolt")
    o.add_argument("--bolt-arg", action="append", default=[])
    o.add_argument("--bolt-mode", choices=["auto", "standard", "recover"], default="auto")
    o.add_argument("--allow-stripped", action="store_true", help="permit BOLT to recover functions from a stripped ELF")
    o.add_argument("--branch-stack", action="store_true")
    o.add_argument("--validate", type=int, default=3, metavar="PAIRS")
    o.add_argument("--metric", help="named ROI metric emitted by tgo.roi.region")
    o.add_argument("--timeout", type=float, default=300)
    t = sub.add_parser("tune", parents=[r], add_help=False,
                       help="record, compare three layouts and publish a confirmed ELF entry")
    t.add_argument("--run", help="reuse an existing capture instead of recording")
    t.add_argument("--train-runs", type=int, default=3, help="fresh training repetitions; existing --run captures are reused unchanged")
    t.add_argument("--entry")
    t.add_argument("--entry-mode", choices=["command", "executable"], default="command")
    t.add_argument("--image", action="append", default=[])
    t.add_argument("--top", type=int, default=3)
    t.add_argument("--recipe", choices=["auto", "standard", "eager-v12"], default="auto")
    t.add_argument("--bolt")
    t.add_argument("--perf2bolt")
    t.add_argument("--bolt-arg", action="append", default=[])
    t.add_argument("--bolt-mode", choices=["auto", "standard", "recover"], default="auto")
    t.add_argument("--allow-stripped", action="store_true", help="permit BOLT to recover functions from a stripped ELF")
    t.add_argument("--search-pairs", type=int, default=3)
    t.add_argument("--validate", type=int, default=5)
    t.add_argument("--metric")
    t.add_argument("--minimum-gain", type=float, default=0.5)
    t.add_argument("--layout", choices=["ext-tsp", "compact", "aligned"], action="append")
    t.add_argument("--image-search", choices=["adaptive", "joint", "all"], default="adaptive")
    t.add_argument("--confirm-candidates", type=int, default=2, help="maximum independently confirmed candidates")
    t.add_argument("--strategy", choices=["auto", "bolt", "go-compiler", "cpython"], default="auto")
    t.add_argument("--python-source", help="matching CPython source archive, verified against the source lock")
    t.add_argument("--python-build", help="reuse a verified completed CPython build for the identical workload")
    t.add_argument("--python-profile", choices=["auto", "native", "sample"], default="auto")
    t.add_argument("--build-jobs", type=int, default=8)
    t.add_argument("--build-timeout", type=float, default=3600)
    t.add_argument("--go", help="Go toolchain used by the compilation workload")
    t.add_argument("--go-cache", choices=["cold", "preserve"], default="cold")
    t.add_argument("--go-variant", choices=["pgo", "compact", "aligned", "hybrid", "layout", "layout-runtime"], action="append")
    t.add_argument("--go-warmups", type=int, default=2, help="unmeasured Go validation warmup pairs")
    v = sub.add_parser("validate", help="AB/BA paired validation of an existing bundle")
    v.add_argument("bundle")
    v.add_argument("--pairs", type=int, default=5)
    v.add_argument("--timeout", type=float, default=300)
    v.add_argument("--metric")
    i = sub.add_parser("inspect", help="show a run/profile/build manifest")
    i.add_argument("path")
    b = sub.add_parser("bind", help="associate an attached profile with its replay command")
    b.add_argument("run")
    b.add_argument("command", nargs=argparse.REMAINDER)
    n = sub.add_parser("normalize", help="reprocess a completed raw capture without rerunning the workload")
    n.add_argument("run")
    n.add_argument("--perf")
    n.add_argument("--allow-truncated", action="store_true")
    m = sub.add_parser("merge", help="combine independently normalized compatible captures")
    m.add_argument("-o", "--output", required=True)
    m.add_argument("--allow-workload-variation", action="store_true")
    m.add_argument("runs", nargs="+")
    args = p.parse_args(argv)
    try:
        if hasattr(args, "command") and args.command[:1] == ["--"]:
            args.command = args.command[1:]
        if args.action == "doctor":
            result = doctor()
        elif args.action == "record":
            if args.delay < 0 or (args.duration is not None and args.duration <= 0):
                p.error("delay must be >=0 and duration >0")
            result = record(args)
        elif args.action == "optimize":
            if args.top <= 0 or args.validate < 0:
                p.error("top must be >0 and validate >=0")
            result = optimize(args)
        elif args.action == "tune":
            if args.top <= 0 or args.search_pairs <= 0 or args.validate <= 0:
                p.error("top, search-pairs and validate must be >0")
            if args.go_warmups < 0:
                p.error("go-warmups must be >=0")
            if args.train_runs <= 0:
                p.error("train-runs must be >0")
            if args.build_jobs <= 0 or args.build_timeout <= 0:
                p.error("build-jobs and build-timeout must be >0")
            if args.confirm_candidates <= 0:
                p.error("confirm-candidates must be >0")
            if args.image_search == "all" and args.top > 4:
                p.error("image-search all requires top <=4")
            reuse_go = args.run and read_json(Path(args.run) / "run.json").get("backend") == "go-cpu"
            if args.strategy == "go-compiler" or (args.strategy == "auto" and (reuse_go or (not args.run and args.command and Path(args.command[0]).name == "go"))):
                from .gopgo import tune_go
                result = tune_go(args)
            else:
                from .tune import tune
                result = tune(args)
        elif args.action == "validate":
            if args.pairs <= 0:
                p.error("pairs must be >0")
            from .validate import validate
            result = validate(Path(args.bundle).resolve(), args.pairs, args.timeout, args.metric)
        elif args.action == "inspect":
            path = Path(args.path)
            if path.is_dir():
                path = next((path / n for n in ("tune.json", "build.json", "profile.json", "run.json") if (path / n).exists()), path)
            result = read_json(path)
        elif args.action == "bind":
            path = Path(args.run) / "run.json"
            result = read_json(path)
            result.setdefault("training_command", result["command"])
            result["command"] = command_path(args.command)
            write_json(path, result)
        elif args.action == "normalize":
            from .util import tool
            root = Path(args.run).resolve()
            state = read_json(root / "run.json")
            if state.get("aggregate"):
                raise ValueError("capture sets reference immutable source runs; use merge to create a refreshed set")
            if state.get("returncode") != 0:
                raise ValueError("raw workload did not finish successfully")
            if state["backend"] == "qemu":
                from .profile import qemu_profile
                result = qemu_profile(root, args.allow_truncated)
            else:
                from .perf import perf_profile
                result = perf_profile(root, tool("perf", args.perf), state.get("pid"))
            state["renormalized"] = {"unix": time.time(), "previous_state": state["state"], "previous_error": state.pop("error", None)}
            state["state"] = "recorded"
            state["phase"] = "complete"
            write_json(root / "run.json", state)
        elif args.action == "merge":
            from .capture_set import merge_captures
            result = merge_captures(args.runs, args.output, args.allow_workload_variation)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError, OSError) as e:
        print(f"tgo: {e}", file=sys.stderr)
        return 1
