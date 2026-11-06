"""通过标准 Linux perf 分支栈采集 BRBE；不访问特权寄存器。"""
from collections import Counter
import math
import os
from pathlib import Path
import platform
import re
import selectors
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time

from .util import (affinity_command, cpu_set, read_json, sha256, terminate_process_group,
                   tool, tool_env, write_json)

BRANCH_FILTER = "any,u"
DEFAULT_POLICY = {"min_samples": 100, "max_loss_percent": 1.0, "max_empty_percent": 10.0}


def record_options(mmap_pages=128):
    if not isinstance(mmap_pages,int) or isinstance(mmap_pages,bool) or not 1<=mmap_pages<=65536 or mmap_pages & (mmap_pages-1):
        raise ValueError('BRBE mmap-pages 必须是 1–65536 范围内的二次幂')
    return ['--sample-cpu',f'--mmap-pages={mmap_pages}','--buildid-all']


def affinity_snapshot(pid):
    rows=[];errors=[]
    try:
        threads=sorted(Path(f'/proc/{pid}/task').iterdir(),key=lambda p:int(p.name))
        for entry in threads[:4096]:
            try:rows.append({'tid':int(entry.name),'allowed_cpus':sorted(os.sched_getaffinity(int(entry.name)))})
            except OSError as error:errors.append({'tid':int(entry.name),'error':str(error)})
        return {'pid':pid,'threads':rows,'thread_count':len(threads),'truncated':len(threads)>4096,'errors':errors}
    except OSError as error:
        return {'pid':pid,'threads':rows,'errors':[str(error)]}


def capabilities(sysfs="/sys/bus/event_source/devices", architecture=None):
    """sysfs 是提示；成功打开事件和实际分支数据才是可用性证明。"""
    architecture = architecture or platform.machine()
    pmus = []
    def read(path):
        try:
            return path.read_text().strip()
        except OSError:
            return None
    for directory in sorted(Path(sysfs).glob("*")):
        depth = read(directory / "caps/branches")
        if "pmuv3" not in directory.name and depth is None:
            continue
        try:
            depth = int(depth, 0) if depth is not None else None
        except ValueError:
            depth = None
        pmus.append({"name": directory.name, "type": read(directory / "type"),
                     "cpus": read(directory / "cpus"), "cpumask": read(directory / "cpumask"),
                     "branch_depth": depth})
    arm = platform.system() == "Linux" and architecture in ("aarch64", "arm64")
    return {"schema": "tgo.brbe-capabilities.v1", "architecture": architecture,
            "kernel": platform.release(), "platform_candidate": arm, "pmus": pmus,
            "advertised": arm and any((p["branch_depth"] or 0) > 0 for p in pmus),
            "allowed_cpus": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else [],
            "perf_event_paranoid": read(Path("/proc/sys/kernel/perf_event_paranoid")),
            "proof_required": "perf event accepted AND nonempty user branch stacks; CPU name/version alone is insufficient"}


def policy(min_samples=100, max_loss_percent=1.0, max_empty_percent=10.0):
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ValueError("BRBE min_samples 必须是正整数")
    for value in (max_loss_percent, max_empty_percent):
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("BRBE 质量百分比阈值必须在 0–100 之间")
    return dict(min_samples=min_samples, max_loss_percent=max_loss_percent, max_empty_percent=max_empty_percent)


def record_counts(path):
    """只扫描 perf 文件节和 UAPI record header/loss；样本仍由 perf 解码。

    参考 tools/perf/Documentation/perf.data-file-format.txt 和 linux/perf_event.h。
    本工具不生成 AUX/compressed 数据；对这些格式明确拒绝，避免漏报损失。
    """
    counts = Counter(); losses = Counter()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("perf.data 必须是普通文件")
        header = stream.read(56)
        if len(header) < 56 or header[:8] not in (b"PERFILE2", b"2ELIFREP"):
            raise ValueError("不支持的 perf.data 文件头；请用 perf record -o FILE 保存原生文件")
        endian = "<" if header[:8] == b"PERFILE2" else ">"
        header_size = struct.unpack_from(endian + "Q", header, 8)[0]
        offset, size = struct.unpack_from(endian + "QQ", header, 40)
        if header_size < 72 or offset < header_size or offset + size > before.st_size:
            raise ValueError("perf.data 节越界或文件被截断（不支持 pipe 格式）")
        end = offset + size
        while offset < end:
            stream.seek(offset)
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError("perf record header 被截断")
            kind, _, length = struct.unpack(endian + "IHH", raw)
            if length < 8 or offset + length > end:
                raise ValueError("perf record 长度无效或被截断")
            if kind in (71, 81, 83):  # PERF_RECORD_AUXTRACE / PERF_RECORD_COMPRESSED{,2}
                raise ValueError("BRBE 质量检查不接受 AUXTRACE/压缩记录；请用本后端直接采集未压缩分支栈")
            counts[kind] += 1
            if kind in (2, 13):
                required = 24 if kind == 2 else 16
                if length < required:
                    raise ValueError("perf LOST record 被截断")
                payload = stream.read(required - 8)
                value = struct.unpack_from(endian + "Q", payload, 8 if kind == 2 else 0)[0]
                losses["lost_records" if kind == 2 else "lost_samples"] += value
            offset += length
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("质量检查期间 perf.data 发生变化")
    return {"schema": "tgo.perf-record-counts.v1", "samples": counts[9],
            "lost_records": losses["lost_records"], "lost_samples": losses["lost_samples"],
            "throttle_records": counts[5], "unthrottle_records": counts[6],
            "record_types": {str(k): v for k, v in sorted(counts.items())}}


class BranchStats:
    sample = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s+\[(\d+)\]\s+(?:0x)?[0-9a-fA-F]+(?:\s+(.*))?$")

    def __init__(self):
        self.counts = Counter(); self.depth = Counter(); self.cpus = Counter(); self.tasks = Counter()
        self.prediction = Counter(); self.excerpt = []

    def feed(self, line):
        if not line.strip() or line.lstrip().startswith("#"):
            return
        match = self.sample.fullmatch(line.rstrip())
        if not match:
            raise ValueError("无法识别 perf script 的 pid,tid,cpu,ip,brstack 输出；见 brbe-script.stderr")
        pid, tid, cpu, branches = match.groups()
        self.counts["samples"] += 1; self.cpus[int(cpu)] += 1
        identity = (int(pid), int(tid))
        if identity in self.tasks or len(self.tasks) < 16384:
            self.tasks[identity] += 1
        else:
            self.counts["untracked_task_samples"] += 1
        entries = (branches or "").split()
        self.depth[len(entries)] += 1
        if entries:
            self.counts["nonempty_samples"] += 1
        for entry in entries:
            fields = entry.split("/")
            if len(fields) < 6 or fields[2] not in ("P", "M", "-"):
                raise ValueError("分支条目不兼容标准 BRBE/BOLT 分支栈格式；禁止把 SPE 合成事件冒充 BRBE")
            try:
                source, target = int(fields[0], 16), int(fields[1], 16)
                int(fields[5])
            except ValueError as error:
                raise ValueError("分支地址或周期字段无效") from error
            self.counts["branches"] += 1
            self.counts["complete_branches" if source and target else "partial_branches"] += 1
            self.prediction[fields[2]] += 1
        if len(self.excerpt) < 8:
            self.excerpt.append(line[:16384].rstrip())

    def result(self):
        samples = self.counts["samples"]
        return {**{k: self.counts[k] for k in ("samples", "nonempty_samples", "branches", "complete_branches", "partial_branches", "untracked_task_samples")},
                "empty_stack_percent": 100 * (samples - self.counts["nonempty_samples"]) / samples if samples else 100.0,
                "depth_histogram": {str(k): v for k, v in sorted(self.depth.items())},
                "max_observed_depth": max(self.depth, default=0),
                "mean_observed_depth": self.counts["branches"] / samples if samples else 0,
                "prediction_flags": dict(self.prediction),
                "samples_per_cpu": {str(k): v for k, v in sorted(self.cpus.items())},
                "tracked_tasks": len(self.tasks), "top_tasks": [dict(pid=p, tid=t, samples=n) for (p, t), n in self.tasks.most_common(64)]}


def _script(command, root, stats, timeout):
    """有截止时间的流式解析，避免把整个分支文本装入内存或落盘。"""
    with (root / "brbe-script.stderr").open("wb") as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, env=tool_env(), start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout; pending = b""
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("perf script BRBE 质量检查超时")
                    for key, _ in selector.select(min(remaining, .25)):
                        block = os.read(key.fd, 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        pending += block
                        lines = pending.split(b"\n"); pending = lines.pop()
                        if len(pending) > 1024 * 1024:
                            raise ValueError("perf script 单行超过 1MiB")
                        for line in lines:
                            stats.feed(line.decode("ascii", errors="replace"))
                if pending:
                    stats.feed(pending.decode("ascii", errors="replace"))
                code = process.wait(timeout=max(.001, deadline-time.monotonic()))
                if code:
                    raise ValueError(f"perf script 失败（{code}）；见 brbe-script.stderr")
        except BaseException:
            terminate_process_group(process)
            raise
        finally:
            process.stdout.close()


def _has_attr(text, name, required, names):
    values = re.findall(r"\b" + name + r"\s*:\s*([^,\n}]+)", text)
    if not values:
        return False
    for value in values:
        try:
            valid = (int(value.strip(), 0) & required) == required
        except ValueError:
            valid = bool(names) and all(re.search(r"\b"+word+r"\b", value) for word in names)
        if not valid:
            return False
    return True


def inspect(root, perf, quality_policy=None, timeout=120):
    root = Path(root); configured = policy(**(quality_policy or DEFAULT_POLICY))
    result = {"schema": "tgo.brbe-quality.v1", "usable": False, "policy": configured, "errors": [],
              "semantics": "sampled recent user branch history; not a complete instruction stream",
              "loss_semantics": "conservative sum of LOST records and LOST_SAMPLES; LOST can include non-sample records; buffer history rollover is not measured loss"}
    commands = [[perf, "evlist", "-v", "-i", str(root / "perf.data")],
                [perf, "script", "--no-itrace", "-i", str(root / "perf.data"), "-F", "pid,tid,cpu,ip,brstack"]]
    stats = BranchStats()
    try:
        def identity():
            value=(root/'perf.data').stat()
            return tuple(getattr(value,key) for key in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))
        initial=identity()
        raw = record_counts(root / "perf.data"); result["records"] = raw
        write_json(root / "perf-record-counts.json", raw)
        loss = raw["lost_records"] + raw["lost_samples"]
        result["loss_upper_bound_percent"] = 100 * loss / (loss + raw["samples"]) if loss + raw["samples"] else 0
        listing = subprocess.run(commands[0], capture_output=True, text=True, env=tool_env(), timeout=timeout)
        (root / "brbe-evlist.txt").write_text(listing.stdout + listing.stderr)
        if listing.returncode:
            raise ValueError("perf evlist 失败；见 brbe-evlist.txt")
        if not _has_attr(listing.stdout, "sample_type", 1 << 11, ["BRANCH_STACK"]):
            raise ValueError("perf.data 没有 PERF_SAMPLE_BRANCH_STACK，不能作为 BRBE 画像")
        if not _has_attr(listing.stdout, "branch_sample_type", 9, ["USER", "ANY"]):
            raise ValueError("perf.data 没有标准 USER|ANY 分支过滤")
        if not _has_attr(listing.stdout, "exclude_kernel", 1, []):
            raise ValueError("BRBE 后端要求用户态采样事件，例如 cycles:u")
        _script(commands[1], root, stats, timeout)
        digest=sha256(root/'perf.data')
        if identity()!=initial:
            raise ValueError('BRBE 检查期间 perf.data 身份/内容发生变化')
        result['raw_data']={'path':str(root/'perf.data'),'sha256':digest}
        result.update(stats.result())
        if result["samples"] != raw["samples"]:
            result["errors"].append("perf script 与原始 SAMPLE 数量不符，不能忽略无法解码的样本")
        if result["nonempty_samples"] < configured["min_samples"]:
            result["errors"].append("非空分支样本不足；延长 ROI/探测时间或降低采样周期")
        if not result["complete_branches"]:
            result["errors"].append("没有同时具备源地址和目标地址的分支")
        if result["empty_stack_percent"] > configured["max_empty_percent"]:
            result["errors"].append("空分支栈比例过高")
        if result["loss_upper_bound_percent"] > configured["max_loss_percent"]:
            result["errors"].append("记录损失超过本工具阈值；降低频率或增加缓冲区")
        if raw["throttle_records"]:
            result["errors"].append("内核对采样进行了节流；降低采样频率后重采")
        result["usable"] = not result["errors"]
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        result.update(stats.result()); result["errors"].append(str(error))
    finally:
        (root / "brbe-samples.txt").write_text("\n".join(stats.excerpt) + "\n")
        write_json(root / "brbe-inspection.json", {"schema": "tgo.brbe-inspection.v1", "commands": commands})
        write_json(root / "brbe-quality.json", result)
    return result


def probe(output, perf=None, cpus=None, event="cycles:u", frequency=999, duration=.5, mmap_pages=128):
    if not math.isfinite(duration) or not .1 <= duration <= 30 or frequency < 1:
        raise ValueError("探测 duration 必须在 0.1–30 秒之间，frequency 必须为正数")
    cpus = cpu_set(cpus)
    options=record_options(mmap_pages)
    root = Path(output).absolute(); root.mkdir(parents=True, exist_ok=False)
    found = capabilities(); write_json(root / "brbe-capabilities.json", found)
    result = {"schema": "tgo.brbe-probe.v1", "usable": False, "capabilities": found,
              "requested_cpus": cpus, "affinity_scope": "only probe child; target business process is not modified"}
    try:
        if not found["platform_candidate"]:
            raise ValueError("BRBE 需要 Linux AArch64；当前平台不符合")
        perf = tool("perf", perf)
        version = subprocess.run([perf, "version"], capture_output=True, text=True, env=tool_env(), timeout=10)
        (root / "brbe-perf-version.txt").write_text(version.stdout + version.stderr)
        # A real user-space branch workload; /bin/true only tests event creation.
        workload = [sys.executable, "-c", "import time\nx=1\nend=time.monotonic()+" + repr(duration) +
                    "\nwhile time.monotonic()<end:\n for i in range(2000):\n  x=((x*1664525+1013904223)^(i if x&1 else ~i))&0xffffffff\nprint(x)\n"]
        command = [perf, "record", "--no-buildid-cache", *options, "-o", str(root / "perf.data"),
                   "-e", event, "-F", str(frequency), "-j", BRANCH_FILTER, "--", *affinity_command(workload, cpus)]
        result["collector_command"] = command
        with (root / "brbe-record.stderr").open("wb") as errors, (root / "brbe-record.stdout").open("wb") as stdout:
            process = subprocess.Popen(command, stdout=stdout, stderr=errors, env=tool_env(), start_new_session=True)
            try:
                result["returncode"] = process.wait(timeout=duration + 20)
            except BaseException:
                terminate_process_group(process); raise
        if result["returncode"]:
            raise ValueError("BRBE 事件未成功采集；见 brbe-record.stderr（权限、内核驱动、固件和虚拟化均可能影响）")
        quality = inspect(root, perf, policy(min_samples=20))
        result.update(usable=quality["usable"], quality=quality,
                      tested_cpus=sorted(int(x) for x in quality.get("samples_per_cpu", {})),
                      scope="只证明本次采样实际覆盖的 CPU/当前事件可用，不代表所有核心或所有内核支持")
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    write_json(root / "brbe-probe.json", result)
    return result


def diagnostic_pack(directory, output):
    """白名单诊断包；不包含负载 argv/env、业务 stdout、perf.data 或二进制。"""
    root = Path(directory).resolve(); destination = Path(output).absolute()
    if destination.exists():
        raise ValueError("诊断包输出已存在")
    if not (root / "brbe-capabilities.json").is_file() and not (root / "brbe-probe.json").is_file():
        raise ValueError("目录不是本工具的 BRBE 探测/采集输出")
    names = ["brbe-capabilities.json", "brbe-quality.json", "brbe-inspection.json", "brbe-evlist.txt",
             "brbe-script.stderr", "brbe-perf-version.txt", "brbe-probe.json", "brbe-record.stderr", "perf-record-counts.json", "brbe-target-affinity.json", "buildids.stderr"]
    summary = {"schema": "tgo.brbe-diagnostic.v1", "excluded": ["perf.data", "ELF", "workload argv/env", "business stdout/stderr", "branch address excerpt"]}
    if (root / "run.json").is_file():
        state = read_json(root / "run.json")
        summary["run"] = {k: state.get(k) for k in ("backend", "state", "phase", "error", "roi", "cpus", "pid", "sampling", "returncode")}
        command = state.get("collector_command") or []
        summary["collector_options"] = command[:command.index("--")] if "--" in command else command
    with tempfile.TemporaryDirectory(prefix="tgo-brbe-pack-") as temporary:
        staging = Path(temporary); included = []
        for name in names:
            source = root / name
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
                raise ValueError("诊断文件必须是 <=1MiB 的普通文件: " + name)
            (staging / name).write_bytes(source.read_bytes()); included.append(name)
        summary["files"] = {name: sha256(staging / name) for name in included}
        write_json(staging / "diagnostic.json", summary)
        # Exclusive creation prevents replacing an existing user artifact.
        with destination.open("xb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for path in sorted(staging.iterdir()):
                    archive.add(path, arcname=path.name, recursive=False)
    return {"schema": "tgo.brbe-pack.v1", "path": str(destination), "sha256": sha256(destination), "bytes": destination.stat().st_size}
