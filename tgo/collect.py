import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import tempfile
from .runtime import build_runtime
from .profile import qemu_profile
from .util import command_path, tool, write_json, read_json, workload_env, tool_env, sha256, terminate_process_group,cpu_set,affinity_command,dependency_asset
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


def doctor(perf_override=None):
    perf = tool("perf", perf_override, required=False)
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
    compiler_inputs = None
    if os.environ.get("TGO_PACKAGED"):
        appdir = os.environ.get("TGO_APPDIR")
        compiler_inputs = {"appdir_exported": bool(appdir), "source_matches_lock": False,
                           "bolt_runtime_matches_manifest": False}
        if appdir:
            appdir = Path(appdir)
            locked = read_json(appdir / "dependencies.lock.json")["python-gzip"]
            source = appdir / "usr/share/tgo/sources" / locked["url"].rsplit("/", 1)[-1]
            archive = appdir / "usr/lib/tgo-bolt/libbolt_rt_instr.a"
            expected = read_json(appdir / "package-manifest.json")["compiler_inputs"]["bolt_runtime"]["sha256"]
            compiler_inputs.update(source_archive=str(source), bolt_runtime=str(archive),
                source_matches_lock=source.is_file() and sha256(source)==locked["sha256"],
                bolt_runtime_matches_manifest=archive.is_file() and sha256(archive)==expected)
    online_platform=platform.system()=='Linux' and platform.machine()=='aarch64' and os.sysconf('SC_PAGE_SIZE')==4096
    from . import brbe
    brbe_capabilities = brbe.capabilities()
    brbe_probe = None
    if perf and brbe_capabilities['platform_candidate']:
        with tempfile.TemporaryDirectory(prefix='tgo-brbe-probe-') as directory:
            brbe_probe = brbe.probe(Path(directory)/'probe', perf=perf)
    return {'schema':'tgo.doctor.v1',"architecture": platform.machine(), "perf": perf, "pmu": pmu, "perf_probe": probe,
            'brbe_capabilities': brbe_capabilities, 'brbe_probe': brbe_probe,
            "spe_probe": spe_probe,
            "packaged_compiler_inputs": compiler_inputs,
            'sdk_assets':{'qemu_header':os.environ.get('TGO_QEMU_HEADER') or dependency_asset('include/qemu-plugin.h'),
                'bolt_instrumentation_archive':os.environ.get('TGO_BOLT_RUNTIME') or dependency_asset('lib/libbolt_rt_instr.a')},
            'online':{'platform_candidate':online_platform,'runtime_gate_required':True,
                'default_generator':tool('llvm-bolt-live',required=False),
                'native_gate_command':'live native-check','python_command':'live python',
                'scope':'自有进程；函数身份/原状态/CFI/ROI内硬件命中/回滚通过后才计为支持'},
            "tools": {n: tool(n, required=False) for n in ("cc", "llvm-bolt", "perf2bolt", "qemu-" + platform.machine(), "patchelf",'readelf','objdump')}}


def record(args, training_images=(), *, cwd=None):
    root = Path(args.output).absolute()
    root.mkdir(parents=True, exist_ok=False)
    overrides = dict(item.split("=", 1) for item in args.env)
    directory=str(Path(cwd or os.getcwd()).resolve())
    resolution_environment=workload_env();resolution_environment.update(overrides)
    command = command_path(args.command,cwd=directory,environment=resolution_environment) if not args.pid else args.command
    if args.pid and args.backend == "qemu":
        raise ValueError("QEMU cannot attach; use perf/spe for an existing process")
    backend = args.backend
    automatic = None
    event_override = args.event
    user_command=command[:]
    cpus=cpu_set(getattr(args,'cpus',None))
    if args.pid and cpus:raise ValueError('--cpus只适用于启动负载；attach不修改已有进程亲和性')
    if args.pid is not None and args.pid<=0:raise ValueError('--pid 必须是正整数')
    if args.pid and args.command:raise ValueError('--pid 与启动命令二选一；回放命令请用 tgo bind 关联')
    adapters=[]
    if command and Path(command[0]).name=='taskset' and len(command)>=4 and command[1] in ('-c','--cpu-list'):
        requested=cpu_set(command[2])
        if cpus and cpus!=requested:raise ValueError('--cpus与taskset包装器的CPU列表冲突')
        cpus=requested;command=command_path(command[3:],cwd=directory,environment=resolution_environment)
        adapters.append({'kind':'taskset-cpu-list','effect':'移到采集器外层，避免QEMU中的exec丢失画像'})
    if backend == "auto":
        d = doctor(args.perf)
        if args.event:
            backend = "spe" if args.event.startswith("arm_spe_") else ('brbe' if args.branch_stack and d.get('brbe_probe',{} ) and d['brbe_probe']['usable'] else "perf")
            reason = "explicit sampling event"
        elif d.get('brbe_probe') and d['brbe_probe']['usable']:
            backend, reason = 'brbe', 'standard user branch-stack probe produced usable samples'
        elif d["spe_probe"] and d["spe_probe"]["usable"]:
            backend, event_override = "spe", d["spe_probe"]["event"]
            reason = "retired branch SPE recorder and buffers opened successfully"
        elif d["perf_probe"] and d["perf_probe"]["usable"]:
            backend, reason = "perf", "cycles recorder opened successfully"
        else:
            backend, reason = "qemu", "hardware sampling probes unavailable"
        automatic = {"selected": backend, "reason": reason,
                     "perf_probe": d["perf_probe"], "spe_probe": d["spe_probe"], 'brbe_probe':d.get('brbe_probe')}
    if backend == 'brbe':
        args.branch_stack = True
    if args.pid and backend == "qemu":
        raise ValueError("perf PMU access unavailable; attach requires perf permissions")
    if getattr(args, "max_roi_windows", 0) and backend != "qemu":
        raise ValueError("max-roi-windows currently applies to QEMU marker recording")
    if args.pid:
        # Hardware attach uses perf's existing MMAP/build-id records. It does
        # not inject our runtime into the target and needs no C compiler.
        runtime=root/'runtime';runtime.mkdir()
    else:runtime = build_runtime(root / "runtime", args.qemu_header if backend == "qemu" else None)
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
               TGO_ROI_LIBRARY=str(runtime / "libtgo_roi.so"), TGO_RESULT_FILE=str(root / "capture.result.json"))
    # These are captured for replay; credentials and unrelated environment are never serialized.
    if cpus:overrides['TGO_WORKLOAD_CPUS']=cpus
    env.update(overrides)
    if training_images:
        from .optimize import verify_images
        verify_images(training_images)
        mapping = root / "training-redirect.tsv"
        for row in training_images:
            if any(c in row[key] for c in "\t\n" for key in ("original", "optimized")):
                raise ValueError("training DSO paths cannot contain tabs/newlines")
        mapping.write_text(''.join(f"{r['original']}\t{r['optimized']}\n" for r in training_images))
        env["TGO_REDIRECT"] = str(mapping)
    env["PYTHONPATH"] = str(runtime / "support") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    meta = {"schema": "tgo.run.v1", "command": command, "user_command": user_command, "cwd": directory, "env": overrides,
            "cpus":cpus,"launch_adapters":adapters,
            "backend": backend, "roi": args.roi, "pid": args.pid, "state": "recording",
            "branch_stack": args.branch_stack,
            "automatic_backend": automatic,
            "started_unix": time.time()}
    meta["roi_window_limit"] = getattr(args, "max_roi_windows", 0)
    if training_images:
        meta["training_images"] = list(training_images)
    write_json(root / "run.json", meta)
    try:
        if backend == 'brbe':
            from . import brbe
            found = brbe.capabilities()
            write_json(root/'brbe-capabilities.json', found)
            if not found['platform_candidate']:
                raise ValueError('BRBE 需要 Linux AArch64；显式请求不回退到其它采集后端')
            meta['brbe_policy'] = brbe.policy(getattr(args,'brbe_min_samples',100),
                getattr(args,'brbe_max_loss_percent',1.0),getattr(args,'brbe_max_empty_percent',10.0))
            brbe_options=brbe.record_options(getattr(args,'brbe_mmap_pages',128))
            if args.frequency < 1 or args.period is not None and args.period < 1:
                raise ValueError('BRBE frequency/period 必须为正数')
            meta['collector_allowed_cpus'] = sorted(os.sched_getaffinity(0))
            if args.pid:
                write_json(root/'brbe-target-affinity.json',{'schema':'tgo.brbe-affinity.v1','before':brbe.affinity_snapshot(args.pid)})
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
            if getattr(args, "max_roi_windows", 0):
                if args.roi != "marker":
                    raise ValueError("limited QEMU ROI windows require marker mode")
                plugin += f",windows={args.max_roi_windows}"
            if "," in str(root):
                raise ValueError("QEMU output directory cannot contain a comma")
            launch = [qemu, "-plugin", plugin]
            for k, v in forwarded.items():
                if k != "LD_AUDIT":
                    launch += ["-E", f"{k}={v}"]
            for k in ("TGO_IMAGE_LOG", "TGO_ROI_LOG", "TGO_ROI_LIBRARY"):
                launch += ["-E", f"{k}={env[k]}"]
            if training_images:
                launch += ["-E", f"TGO_REDIRECT={env['TGO_REDIRECT']}"]
            launch += ["-E", f"LD_AUDIT={runtime / 'libtgo_audit.so'}", *command]
            launch=affinity_command(launch,cpus)
            meta["sampling"] = {"backend": "qemu", "event": None, "period": None,
                                "frequency": None, "branch_stack": bool(args.branch_stack)}
        else:
            perf = tool("perf", args.perf)
            event = event_override or ("arm_spe_0" + SPE_FILTER if backend == "spe" else "cycles:u")
            meta["sampling"] = {"backend": backend, "event": event,
                "period": (args.period or 4096) if backend == "spe" or args.period else None,
                "frequency": args.frequency if backend != "spe" and not args.period else None,
                "branch_stack": bool(args.branch_stack)}
            if backend == 'brbe':
                meta['sampling']['branch_filter'] = brbe.BRANCH_FILTER
            # Keep build IDs in perf.data, but do not hardlink source files into
            # perf's global cache. On some overlayfs hosts those cache writes
            # make later kernel MMAP names appear as '/ (deleted)'.
            launch = [perf, "record", "--no-buildid-cache", "-o", str(root / "perf.data"), "-e", event]
            if backend == "spe":
                launch += SPE_RECORD_OPTIONS
            elif backend == 'brbe':
                launch += brbe_options
                version=subprocess.run([perf,'version'],capture_output=True,text=True,env=tool_env(),timeout=10)
                (root/'brbe-perf-version.txt').write_text(version.stdout+version.stderr)
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
                launch += ["--", *affinity_command(["env", *inherited, f"LD_AUDIT={runtime / 'libtgo_audit.so'}", *command],cpus)]
        meta["collector_command"] = launch
        meta["collector_sha256"] = sha256(launch[0])
        meta['collector_binary']=qemu if backend=='qemu' else perf
        meta['collector_binary_sha256']=sha256(meta['collector_binary'])
        meta["runtime_sha256"] = {p.name: sha256(p) for p in runtime.glob("*.so")}
        write_json(root / "run.json", meta)
        with (root / "stdout").open("wb") as stdout, (root / "stderr").open("wb") as stderr:
            p = subprocess.Popen(launch, stdout=stdout, stderr=stderr, env=exec_env, cwd=meta["cwd"], start_new_session=True)
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
        if (root / "capture.result.json").exists():
            import hashlib, json
            meta["correctness_sha256"] = hashlib.sha256(json.dumps(read_json(root / "capture.result.json"),
                sort_keys=True, allow_nan=False).encode()).hexdigest()
        if training_images:
            verify_images(training_images)
    except BaseException as e:
        if isinstance(e, subprocess.TimeoutExpired):
            message = f"workload exceeded --timeout ({args.timeout:g}s); capture preserved at {root}"
            meta.update(state="failed", error=message)
            raise RuntimeError(message) from e
        meta.update(state="failed", error=str(e)); raise
    finally:
        if backend=='brbe' and args.pid and (root/'brbe-target-affinity.json').exists():
            affinity=read_json(root/'brbe-target-affinity.json');affinity['after']=brbe.affinity_snapshot(args.pid)
            write_json(root/'brbe-target-affinity.json',affinity)
        write_json(root / "run.json", meta)
    return meta
