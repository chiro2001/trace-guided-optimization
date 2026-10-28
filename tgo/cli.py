import argparse
import json
import os
from pathlib import Path
import sys
import time
from .collect import doctor, record
from .optimize import optimize
from .util import read_json, write_json, command_path


def main(argv=None):
    p = argparse.ArgumentParser(prog="tgo", description="ROI-driven native code layout optimization")
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
    o.add_argument("--bolt")
    o.add_argument("--perf2bolt")
    o.add_argument("--bolt-arg", action="append", default=[])
    o.add_argument("--branch-stack", action="store_true")
    o.add_argument("--validate", type=int, default=3, metavar="PAIRS")
    o.add_argument("--metric", help="named ROI metric emitted by tgo.roi.region")
    o.add_argument("--timeout", type=float, default=300)
    t = sub.add_parser("tune", parents=[r], add_help=False,
                       help="record, compare three layouts and publish a confirmed ELF entry")
    t.add_argument("--run", help="reuse an existing capture instead of recording")
    t.add_argument("--entry")
    t.add_argument("--entry-mode", choices=["command", "executable"], default="command")
    t.add_argument("--image", action="append", default=[])
    t.add_argument("--top", type=int, default=3)
    t.add_argument("--bolt")
    t.add_argument("--perf2bolt")
    t.add_argument("--bolt-arg", action="append", default=[])
    t.add_argument("--search-pairs", type=int, default=3)
    t.add_argument("--validate", type=int, default=5)
    t.add_argument("--metric")
    t.add_argument("--minimum-gain", type=float, default=0.5)
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
                path = next((path / n for n in ("build.json", "profile.json", "run.json") if (path / n).exists()), path)
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
            write_json(root / "run.json", state)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError, OSError) as e:
        print(f"tgo: {e}", file=sys.stderr)
        return 1
