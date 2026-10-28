import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import time
from .runtime import build_runtime
from .profile import qemu_profile
from .util import command_path, tool, write_json, workload_env, tool_env


def doctor():
    perf = tool("perf", required=False)
    pmu = {p.name: (p / "type").read_text().strip() for p in Path("/sys/bus/event_source/devices").glob("*") if (p / "type").exists()}
    probe = None
    if perf:
        r = subprocess.run([perf, "stat", "-e", "cycles:u", "--", "true"], capture_output=True, text=True, env=tool_env())
        probe = {"usable": r.returncode == 0, "diagnostic": r.stderr.strip()}
    return {"architecture": platform.machine(), "perf": perf, "pmu": pmu, "perf_probe": probe,
            "tools": {n: tool(n, required=False) for n in ("cc", "llvm-bolt", "perf2bolt", "qemu-" + platform.machine(), "patchelf")}}


def record(args):
    root = Path(args.output).absolute()
    root.mkdir(parents=True, exist_ok=False)
    command = command_path(args.command) if not args.pid else args.command
    if args.pid and args.backend == "qemu":
        raise ValueError("QEMU cannot attach; use perf/spe for an existing process")
    backend = args.backend
    if backend == "auto":
        d = doctor()
        backend = "perf" if d["perf_probe"] and d["perf_probe"]["usable"] else "qemu"
    if args.pid and backend == "qemu":
        raise ValueError("perf PMU access unavailable; attach requires perf permissions")
    runtime = build_runtime(root / "runtime", args.qemu_header if backend == "qemu" else None)
    user_command = command[:]
    if args.python_hook:
        if args.pid:
            raise ValueError("Python hooks require launch mode")
        support = runtime / "support/tgo"
        support.mkdir(parents=True)
        for name in ("__init__.py", "roi.py", "pmu.py", "python_entry.py"):
            shutil.copy2(Path(__file__).parent / name, support / name)
        command = [command[0], str(support / "python_entry.py"), "--hook", args.python_hook, "--", *command[1:]]
        args.roi = "marker"
    env = workload_env()
    env.update(TGO_IMAGE_LOG=str(root / "capture"), TGO_ROI_LOG=str(root / "capture"),
               TGO_ROI_LIBRARY=str(runtime / "libtgo_roi.so"))
    # These are captured for replay; credentials and unrelated environment are never serialized.
    overrides = dict(item.split("=", 1) for item in args.env)
    env.update(overrides)
    meta = {"schema": "tgo.run.v1", "command": command, "user_command": user_command, "cwd": os.getcwd(), "env": overrides,
            "backend": backend, "roi": args.roi, "pid": args.pid, "state": "recording",
            "started_unix": time.time()}
    write_json(root / "run.json", meta)
    try:
        exec_env = env.copy()
        forwarded = {}
        for key in ("LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "PYTHONPLATLIBDIR"):
            if key in exec_env:
                forwarded[key] = exec_env.pop(key)
        if backend == "qemu":
            if args.roi == "window":
                raise ValueError("QEMU uses whole or marker ROI; time windows/attach require perf")
            if not (runtime / "libtgo_qemu.so").exists():
                raise ValueError("QEMU plugin header required: --qemu-header or TGO_QEMU_HEADER; bootstrap downloads it")
            qemu = tool("qemu-" + platform.machine(), args.qemu)
            plugin = f"{runtime / 'libtgo_qemu.so'},out={root / 'capture'},roi={args.roi},limit={args.max_insns}"
            if "," in str(root):
                raise ValueError("QEMU output directory cannot contain a comma")
            launch = [qemu, "-plugin", plugin]
            for k, v in forwarded.items():
                if k != "LD_AUDIT":
                    launch += ["-E", f"{k}={v}"]
            for k in ("TGO_IMAGE_LOG", "TGO_ROI_LOG", "TGO_ROI_LIBRARY"):
                launch += ["-E", f"{k}={env[k]}"]
            launch += ["-E", f"LD_AUDIT={runtime / 'libtgo_audit.so'}", *command]
        else:
            perf = tool("perf", args.perf)
            event = args.event or ("arm_spe_0/branch_filter=1/" if backend == "spe" else "cycles:u")
            launch = [perf, "record", "-o", str(root / "perf.data"), "-e", event]
            if backend == "spe":
                launch += ["--switch-events", "--sample-cpu", "--mmap-pages=1024,16384"]
            if backend != "spe":
                launch += ["-F", str(args.frequency)]
            if args.branch_stack:
                launch += ["-j", "any,u"]
            if args.roi == "marker":
                if args.pid:
                    raise ValueError("marker attach requires an already configured control FIFO; use window attach")
                ctl, ack = root / "control.fifo", root / "ack.fifo"
                os.mkfifo(ctl); os.mkfifo(ack)
                launch += ["--delay=-1", "--control=fifo:" + str(ctl) + "," + str(ack)]
                env.update(TGO_PERF_CONTROL=str(ctl), TGO_PERF_ACK=str(ack))
            elif args.roi == "window":
                if args.duration is None:
                    raise ValueError("--roi window requires --duration")
                launch += [f"--delay={int(args.delay * 1000)}-{int((args.delay + args.duration) * 1000)}"]
            if args.pid:
                if args.duration is None:
                    raise ValueError("attach requires --duration")
                launch += ["-p", str(args.pid), "--", "sleep", str(args.delay + args.duration)]
            else:
                # Only the workload gets LD_AUDIT; perf's own maps do not pollute discovery.
                inherited = [f"{k}={v}" for k, v in forwarded.items() if k != "LD_AUDIT"]
                launch += ["--", "env", *inherited, f"LD_AUDIT={runtime / 'libtgo_audit.so'}", *command]
        meta["collector_command"] = launch
        write_json(root / "run.json", meta)
        with (root / "stdout").open("wb") as stdout, (root / "stderr").open("wb") as stderr:
            p = subprocess.Popen(launch, stdout=stdout, stderr=stderr, env=exec_env, start_new_session=True)
            try:
                code = p.wait(timeout=args.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL); p.wait()
                raise
        meta["returncode"] = code
        if code:
            raise RuntimeError(f"collector/workload failed ({code}); see {root / 'stderr'}")
        if backend == "qemu":
            profile = qemu_profile(root, args.allow_truncated)
            if args.roi == "marker" and not profile["marker_windows"]:
                raise ValueError("no explicit ROI windows were observed")
        else:
            # perf2bolt consumes mmap/build-id records directly, including attach/child processes.
            from .perf import perf_profile
            perf_profile(root, perf, args.pid)
        meta.update(state="recorded", finished_unix=time.time())
    except BaseException as e:
        meta.update(state="failed", error=str(e)); raise
    finally:
        write_json(root / "run.json", meta)
    return meta
