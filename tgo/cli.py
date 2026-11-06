import argparse
import json
import os
from pathlib import Path
import sys
import time
import signal
import threading
from . import __version__
from .collect import doctor, record
from .optimize import optimize
from .util import read_json, write_json, command_path,dependency_asset


def main(argv=None):
    if threading.current_thread() is threading.main_thread():
        def terminate(_signum,_frame):raise KeyboardInterrupt('任务收到终止信号')
        signal.signal(signal.SIGTERM,terminate)
    p = argparse.ArgumentParser(prog="tgo", description="ROI-driven native code layout optimization")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="action", required=True)
    d=sub.add_parser("doctor", help="probe tools and PMU access")
    d.add_argument('--perf')
    brbe=sub.add_parser('brbe',help='标准perf BRBE实机预检与有限诊断包')
    bc=brbe.add_subparsers(dest='brbe_action',required=True)
    probe=bc.add_parser('probe',help='真实用户态分支工作集，验证权限/驱动/分支数据')
    probe.add_argument('-o','--output',required=True);probe.add_argument('--perf');probe.add_argument('--cpus')
    probe.add_argument('--event',default='cycles:u');probe.add_argument('--frequency',type=int,default=999)
    probe.add_argument('--duration',type=float,default=.5)
    probe.add_argument('--mmap-pages',type=int,default=128)
    pack=bc.add_parser('pack',help='不包含业务argv/env、原始perf数据或ELF的诊断包')
    pack.add_argument('directory');pack.add_argument('-o','--output',required=True)
    sub.add_parser('agent',help='启动MCP stdio工具接口，标准输出仅输出JSON-RPC')
    c = sub.add_parser("capabilities", help="inspect ELF/runtime structure without a known-input hash allowlist")
    c.add_argument("image")
    prepare=sub.add_parser('prepare',help='准备不可覆盖的新ELF输入，裁剪调试信息时保留FILE和函数符号')
    prepare.add_argument('image');prepare.add_argument('-o','--output',required=True)
    prepare.add_argument('--strip-debug',action='store_true');prepare.add_argument('--objcopy')
    j=sub.add_parser('job',help='启动、查询或取消持久任务，保留原始日志')
    jobs=j.add_subparsers(dest='job_action',required=True)
    start=jobs.add_parser('start');start.add_argument('-o','--output',required=True);start.add_argument('--cwd')
    start.add_argument('--env',action='append',default=[]);start.add_argument('--timeout',type=float)
    start.add_argument('--mem-floor-gib',type=float)
    start.add_argument('--wait-for',action='append',default=[],help='等待其它任务进入任意终态，串行化测量')
    start.add_argument('command',nargs=argparse.REMAINDER)
    for name in ['status','stop']:
        child=jobs.add_parser(name);child.add_argument('directory')
    e=sub.add_parser('experiment',help='跨负载训练、独立确认与留出验证')
    experiments=e.add_subparsers(dest='experiment_kind',required=True)
    python_experiment=experiments.add_parser('python')
    from .workloads.python_campaign import options as python_options
    python_options(python_experiment)
    from .workloads.stdlib_campaign import options as stdlib_options
    stdlib_options(experiments.add_parser('python-stdlib',help='真实标准库AST与字节码编译，使用不同模块子集留出'))
    from .workloads.gem5_campaign import options as gem5_options
    gem5_options(experiments.add_parser('gem5',help='gem5宿主原生wall调优，guest校验和与仿真计数验证语义'))
    w=sub.add_parser('workload',help='列出或运行内置跨负载工作集')
    w.add_argument('kind',choices=['list','python','go','gem5'])
    w.add_argument('workload_args',nargs=argparse.REMAINDER)
    live=sub.add_parser('live',help='受控AArch64在线代码替换验证，保留原数据与旧帧')
    live_commands=live.add_subparsers(dest='live_action',required=True)
    gate=live_commands.add_parser('native-check');gate.add_argument('-o','--output',required=True)
    gate.add_argument('--bolt',required=True);gate.add_argument('--python');gate.add_argument('--instrument-bolt');gate.add_argument('--runtime');gate.add_argument('--cpus')
    gate.add_argument('--capsule',action='store_true')
    from .live.python_replay import options as live_python_options
    live_python_options(live_commands.add_parser('python',help='已有画像在自有长驻Python中在线构建、AB/BA与回滚'))
    live_python_options(live_commands.add_parser('stdlib',help='真实标准库AST/字节码编译的长驻进程在线验证'),workload='python-stdlib')
    from .live.campaign import options as live_campaign_options
    live_campaign_options(live_commands.add_parser('tune-python',help='在线布局筛选、独立确认与新seed留出，未通过则保留原版'))
    live_campaign_options(live_commands.add_parser('tune-stdlib',help='真实标准库在线调优，独立确认与不同模块子集留出'),workload='python-stdlib')
    r = sub.add_parser("record", help="record a workload or attach to a PID")
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--backend", choices=["auto", "brbe", "perf", "spe", "qemu"], default="auto")
    r.add_argument("--roi", choices=["whole", "marker", "window"], default="whole")
    r.add_argument("--pid", type=int)
    r.add_argument("--duration", type=float)
    r.add_argument("--delay", type=float, default=0)
    r.add_argument("--frequency", type=int, default=999)
    r.add_argument("--period", type=int, help="sample period; SPE defaults to 4096")
    r.add_argument("--event")
    r.add_argument("--branch-stack", action="store_true")
    r.add_argument('--brbe-min-samples',type=int,default=100,help='至少有多少非空分支栈样本')
    r.add_argument('--brbe-max-loss-percent',type=float,default=1.0)
    r.add_argument('--brbe-max-empty-percent',type=float,default=10.0)
    r.add_argument('--brbe-mmap-pages',type=int,default=128,help='标准perf数据环缓冲页数，须为二次幂')
    r.add_argument("--python-hook", metavar="MODULE:CLASS.METHOD")
    r.add_argument('--cpus',help='工作负载允许的CPU列表；采集控制器保持自身亲和性')
    r.add_argument("--qemu", default=os.environ.get("TGO_QEMU"))
    r.add_argument("--perf", default=os.environ.get("TGO_PERF"))
    r.add_argument("--qemu-header", default=os.environ.get("TGO_QEMU_HEADER") or dependency_asset('include/qemu-plugin.h'))
    r.add_argument("--max-insns", type=int, default=1000000000)
    r.add_argument("--max-roi-windows", type=int, default=0, help="QEMU marker windows to record; 0 records all, workload still executes completely")
    r.add_argument("--allow-truncated", action="store_true")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("command", nargs=argparse.REMAINDER)
    o = sub.add_parser("optimize", help="build an immutable DSO bundle and native ELF entry")
    o.add_argument("--run", required=True)
    o.add_argument("-o", "--output", required=True)
    o.add_argument("--entry", help="new ELF path, e.g. /path/to/python3.tgo; never overwrites")
    o.add_argument("--entry-mode", choices=["command", "executable"], default="command")
    o.add_argument('--publish-affinity',choices=['inherit','workload'],default='inherit',help='发布入口默认继承调用者CPU集合；workload显式保留测试CPU集合')
    o.add_argument("--image", action="append", default=[])
    o.add_argument("--top", type=int, default=3)
    o.add_argument("--recipe", choices=["auto", "standard", "generic", "eager-v12"], default="auto", help="eager-v12 is a compatibility alias for structural policy")
    o.add_argument("--optimization-level", choices=["conservative","balanced","aggressive"], default="balanced")
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
    t.add_argument('--publish-affinity',choices=['inherit','workload'],default='inherit')
    t.add_argument("--image", action="append", default=[])
    t.add_argument("--top", type=int, default=3)
    t.add_argument("--recipe", choices=["auto", "standard", "generic", "eager-v12"], default="auto")
    t.add_argument("--optimization-level", choices=["conservative","balanced","aggressive"], default="balanced")
    t.add_argument("--bolt")
    t.add_argument("--perf2bolt")
    t.add_argument("--bolt-arg", action="append", default=[])
    t.add_argument("--bolt-mode", choices=["auto", "standard", "recover"], default="auto")
    t.add_argument("--allow-stripped", action="store_true", help="permit BOLT to recover functions from a stripped ELF")
    t.add_argument("--search-pairs", type=int, default=3)
    t.add_argument("--validate", type=int, default=5)
    t.add_argument("--metric")
    t.add_argument('--validation-cases',help='多负载argv/环境场景JSON；按最差场景筛选并逐场景确认')
    t.add_argument("--minimum-gain", type=float, default=0.5)
    t.add_argument("--layout", choices=["ext-tsp", "compact", "aligned"], action="append")
    t.add_argument("--image-search", choices=["adaptive", "joint", "all"], default="adaptive")
    t.add_argument("--confirm-candidates", type=int, default=2, help="maximum independently confirmed candidates")
    t.add_argument("--strategy", choices=["auto", "bolt", "go-compiler", "cpython"], default="auto")
    t.add_argument("--python-source", help="matching CPython source archive; validate Include/patchlevel.h against the running interpreter")
    t.add_argument("--python-build", help="reuse a verified completed CPython build for the identical workload")
    t.add_argument("--python-profile", choices=["auto", "native", "sample"], default="auto")
    t.add_argument("--library-capture", help="reuse a verified QEMU capture for matching non-Python DSOs")
    t.add_argument("--library-backend", choices=["auto", "spe", "qemu"], default="auto")
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
    v.add_argument('--cases',help='验证多负载JSON；可声明role=heldout，只复测固定工件')
    h=sub.add_parser('evaluate',help='既定工件在留出负载上独立验证，不重选候选')
    h.add_argument('--bundle',required=True);h.add_argument('-o','--output',required=True);h.add_argument('--pairs',type=int,default=3)
    h.add_argument('--metric');h.add_argument('--timeout',type=float,default=300);h.add_argument('--cwd')
    h.add_argument('--env',action='append',default=[]);h.add_argument('command',nargs=argparse.REMAINDER)
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
        if hasattr(args, "max_roi_windows") and args.max_roi_windows < 0:
            p.error("max-roi-windows must be >=0")
        if args.action == "doctor":
            result = doctor(args.perf)
        elif args.action == 'brbe':
            from .brbe import probe,diagnostic_pack
            if args.brbe_action=='probe':
                result=probe(args.output,args.perf,args.cpus,args.event,args.frequency,args.duration,args.mmap_pages)
            else:result=diagnostic_pack(args.directory,args.output)
        elif args.action=='agent':
            from .agent import serve
            serve();return 0
        elif args.action == "capabilities":
            from .capabilities import inspect_image
            result=inspect_image(args.image)
        elif args.action=='prepare':
            from .prepare import prepare as prepare_input
            result=prepare_input(args.image,args.output,args.strip_debug,args.objcopy)
        elif args.action=='job':
            from .jobs import launch,state,stop
            if args.job_action=='start':
                overrides=dict(value.split('=',1) for value in args.env)
                result=launch(args.command,args.output,args.cwd,overrides,args.timeout,mem_floor_gib=args.mem_floor_gib,wait_for=args.wait_for)
            elif args.job_action=='status':result=state(args.directory)
            else:result=stop(args.directory)
        elif args.action=='experiment':
            from .workloads.python_campaign import run as run_python
            if min(args.size,args.train_rounds,args.rounds,args.search_pairs,args.pairs)<=0 or args.timeout<=0:p.error('工作集规模、轮数与timeout必须为正数')
            if abs(args.seed)==abs(args.heldout_seed):p.error('训练与留出seed规范化后须不同')
            if getattr(args,'modules',1)<=0:p.error('modules须为正数')
            if args.experiment_kind=='python-stdlib':
                from .workloads.stdlib_campaign import run as run_stdlib
                result=run_stdlib(args)
            elif args.experiment_kind=='gem5':
                from .workloads.gem5_campaign import run as run_gem5
                result=run_gem5(args)
            else:result=run_python(args)
        elif args.action=='workload':
            if args.kind=='list':
                root=Path(__file__).parent/'workloads'
                result={'schema':'tgo.workloads.v1','workloads':[
                    {'name':'python-heavy','cases':['dispatch','graph','parsers','mixed'],'entry_script':str(root/'python_heavy.py'),'experiment_command':'experiment python'},
                    {'name':'python-stdlib-compile','entry_script':str(root/'python_stdlib.py'),'experiment_command':'experiment python-stdlib'},
                    {'name':'go-compile','entry_script':str(root/'go_compile.py'),'cache_modes':['cold','preserve'],'optimization_backend':'go-compiler'},
                    {'name':'gem5-o3','entry_script':str(root/'gem5_workload.py'),'simulation_config':str(root/'gem5_se.py'),
                     'metric':'gem5-simulate','optimization_backend':'bolt','scope':'宿主模拟器原生wall'}]}
            else:
                from contextlib import redirect_stdout
                import io
                if args.kind=='python':from .workloads.python_heavy import main as workload_main
                elif args.kind=='gem5':from .workloads.gem5_workload import main as workload_main
                else:from .workloads.go_compile import main as workload_main
                with redirect_stdout(io.StringIO()):result=workload_main(args.workload_args)
        elif args.action=='live':
            if args.live_action=='native-check':
                from .live.native_gate import check
                result=check(args.output,args.bolt,args.python,args.instrument_bolt,args.runtime,args.cpus,args.capsule)
            elif args.live_action in ('python','stdlib'):
                from .live.python_replay import run as live_python
                result=live_python(args)
            else:
                from .live.campaign import run as live_campaign
                result=live_campaign(args)
        elif args.action == "record":
            if args.delay < 0 or (args.duration is not None and args.duration <= 0):
                p.error("delay must be >=0 and duration >0")
            result = record(args)
        elif args.action == "optimize":
            if args.top <= 0 or args.validate < 0:
                p.error("top must be >0 and validate >=0")
            result = optimize(args)
        elif args.action == "tune":
            if args.validation_cases and args.strategy!='bolt':p.error('--validation-cases需要显式--strategy bolt；Go工件可用validate --cases')
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
            if args.cases:
                from .scenarios import read_scenarios,validate_scenarios
                result=validate_scenarios(args.bundle,read_scenarios(args.cases),args.pairs,args.timeout,args.metric)
            else:
                from .validate import validate
                result = validate(Path(args.bundle).resolve(), args.pairs, args.timeout, args.metric)
        elif args.action=='evaluate':
            if args.pairs<=0 or args.timeout<=0:p.error('pairs和timeout必须为正数')
            from .evaluate import heldout
            result=heldout(args.bundle,args.output,args.command,args.pairs,args.metric,args.timeout,args.cwd,
                dict(value.split('=',1) for value in args.env))
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
        if args.action=='brbe' and args.brbe_action=='probe' and not result['usable']:return 1
        return 0
    except (RuntimeError, ValueError, OSError) as e:
        print(f"tgo: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('tgo: 任务已终止，保留已有证据',file=sys.stderr);return 130
