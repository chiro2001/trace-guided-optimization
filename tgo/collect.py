import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import tempfile
from .runtime import build_runtime
from .profile import qemu_profile
from .util import command_path, tool, write_json, workload_env, tool_env, sha256, terminate_process_group
from .validate import execute_trial

SPE_FILTER = "/ts_enable=1,branch_filter=1,event_filter=0x2,jitter=1/u"
SPE_RECORD_OPTIONS = ["--switch-events", "--sample-cpu", "--mmap-pages=1024,16384"]
PERF_RECORD_OPTIONS = ["--mmap-pages=128"]


def probe_command(command):
    with tempfile.TemporaryDirectory(prefix="tgo-probe-") as directory:
        prefix = Path(directory) / "probe"
        try:
            code, _, timed_out, _ = execute_trial(command, prefix, tool_env(), "/", 10)
            diagnostic = Path(str(prefix) + ".stderr").read_text(errors="replace")
            return {"usable": code == 0 and not timed_out, "timed_out": timed_out,
                    "diagnostic": diagnostic.strip()}
        except OSError as error:
            return {"usable": False, "diagnostic": str(error)}


def doctor():
    perf = tool("perf", required=False)
    pmu = {p.name: (p / "type").read_text().strip() for p in Path("/sys/bus/event_source/devices").glob("*") if (p / "type").exists()}
    probe, spe_probe = None, None
    if perf:
        with tempfile.TemporaryDirectory(prefix="tgo-perf-probe-") as directory:
            probe = probe_command([perf, "record", "-o", str(Path(directory) / "perf.data"),
                "--no-buildid-cache", *PERF_RECORD_OPTIONS, "-e", "cycles:u", "-F", "999", "--", "/bin/true"])
        spe_name = next((name for name in sorted(pmu) if name.startswith("arm_spe_")), None)
        if spe_name:
            event = spe_name + SPE_FILTER
            with tempfile.TemporaryDirectory(prefix="tgo-spe-probe-") as directory:
                spe_probe = probe_command([perf, "record", "-o", str(Path(directory) / "perf.data"),
                    "--no-buildid-cache", "-e", event, *SPE_RECORD_OPTIONS, "-c", "4096", "--", "/bin/true"])
            spe_probe.update(event=event, scope="configured recorder/buffer permission probe; workload sampling coverage is checked after recording")
    return {"architecture": platform.machine(), "perf": perf, "pmu": pmu, "perf_probe": probe,
            "spe_probe": spe_probe,
            "tools": {n: tool(n, required=False) for n in ("cc", "llvm-bolt", "perf2bolt", "qemu-" + platform.machine(), "patchelf")}}


def record(args):
    root = Path(args.output).absolute()
    root.mkdir(parents=True, exist_ok=False)
    command = command_path(args.command) if not args.pid else args.command
    if args.pid and args.backend == "qemu":
        raise ValueError("QEMU cannot attach; use perf/spe for an existing process")
    backend = args.backend
    automatic = None
    event_override = args.event
    if backend == "auto":
        d = doctor()
        if args.event:
            backend = "spe" if args.event.startswith("arm_spe_") else "perf"
            reason = "explicit sampling event"
        elif d["spe_probe"] and d["spe_probe"]["usable"]:
            backend, event_override = "spe", d["spe_probe"]["event"]
            reason = "retired branch SPE recorder and buffers opened successfully"
        elif d["perf_probe"] and d["perf_probe"]["usable"]:
            backend, reason = "perf", "cycles recorder opened successfully"
        else:
            backend, reason = "qemu", "hardware sampling probes unavailable"
        automatic = {"selected": backend, "reason": reason,
                     "perf_probe": d["perf_probe"], "spe_probe": d["spe_probe"]}
    if args.pid and backend == "qemu":
        raise ValueError("perf PMU access unavailable; attach requires perf permissions")
    runtime = build_runtime(root / "runtime", args.qemu_header if backend == "qemu" else None)
    user_command = command[:]
    if args.python_hook:
        if args.pid:
            raise ValueError("Python hooks require launch mode")
        support = runtime / "support/tgo"
        support.mkdir(parents=True, exist_ok=True)
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
    env["PYTHONPATH"] = str(runtime / "support") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    meta = {"schema": "tgo.run.v1", "command": command, "user_command": user_command, "cwd": os.getcwd(), "env": overrides,
            "backend": backend, "roi": args.roi, "pid": args.pid, "state": "recording",
            "branch_stack": args.branch_stack,
            "automatic_backend": automatic,
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
            meta["sampling"] = {"backend": "qemu", "event": None, "period": None,
                                "frequency": None, "branch_stack": bool(args.branch_stack)}
        else:
            perf = tool("perf", args.perf)
            event = event_override or ("arm_spe_0" + SPE_FILTER if backend == "spe" else "cycles:u")
            meta["sampling"] = {"backend": backend, "event": event,
                "period": (args.period or 4096) if backend == "spe" or args.period else None,
                "frequency": args.frequency if backend != "spe" and not args.period else None,
                "branch_stack": bool(args.branch_stack)}
            # Keep build IDs in perf.data, but do not hardlink source files into
            # perf's global cache. On some overlayfs hosts those cache writes
            # make later kernel MMAP names appear as '/ (deleted)'.
            launch = [perf, "record", "--no-buildid-cache", "-o", str(root / "perf.data"), "-e", event]
            if backend == "spe":
                launch += SPE_RECORD_OPTIONS
            else:
                launch += PERF_RECORD_OPTIONS
            if backend == "spe" or args.period:
                launch += ["-c", str(args.period or 4096)]
            else:
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
                exec_env.update(TGO_PERF_CONTROL=str(ctl), TGO_PERF_ACK=str(ack))
            elif args.roi == "window":
                if args.duration is None:
                    raise ValueError("--roi window requires --duration")
                launch += [f"--delay={int(args.delay * 1000)}-{int((args.delay + args.duration) * 1000)}"]
                meta["roi_window"] = f"{int(args.delay * 1000)}-{int((args.delay + args.duration) * 1000)}"
            if args.pid:
                if args.duration is None:
                    raise ValueError("attach requires --duration")
                launch += ["-p", str(args.pid), "--", "sleep", str(args.delay + args.duration)]
            else:
                # Only the workload gets LD_AUDIT; perf's own maps do not pollute discovery.
                inherited = [f"{k}={v}" for k, v in forwarded.items() if k != "LD_AUDIT"]
                launch += ["--", "env", *inherited, f"LD_AUDIT={runtime / 'libtgo_audit.so'}", *command]
        meta["collector_command"] = launch
        meta["collector_sha256"] = sha256(launch[0])
        meta["runtime_sha256"] = {p.name: sha256(p) for p in runtime.glob("*.so")}
        write_json(root / "run.json", meta)
        with (root / "stdout").open("wb") as stdout, (root / "stderr").open("wb") as stderr:
            p = subprocess.Popen(launch, stdout=stdout, stderr=stderr, env=exec_env, start_new_session=True)
            try:
                code = p.wait(timeout=args.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                terminate_process_group(p)
                raise
        meta["returncode"] = code
        if code:
            raise RuntimeError(f"collector/workload failed ({code}); see {root / 'stderr'}")
        meta["phase"] = "normalizing"
        write_json(root / "run.json", meta)
        if backend == "qemu":
            profile = qemu_profile(root, args.allow_truncated)
            if args.roi == "marker" and not profile["marker_windows"]:
                raise ValueError("no explicit ROI windows were observed")
        else:
            # perf2bolt consumes mmap/build-id records directly, including attach/child processes.
            from .perf import perf_profile
            perf_profile(root, perf, args.pid)
        meta.update(state="recorded", phase="complete", finished_unix=time.time())
    except BaseException as e:
        if isinstance(e, subprocess.TimeoutExpired):
            message = f"workload exceeded --timeout ({args.timeout:g}s); capture preserved at {root}"
            meta.update(state="failed", error=message)
            raise RuntimeError(message) from e
        meta.update(state="failed", error=str(e)); raise
    finally:
        write_json(root / "run.json", meta)
    return meta
