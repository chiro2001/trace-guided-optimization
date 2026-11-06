#!/usr/bin/env python3
"""只读复算公开的920B vLLM正式矩阵；不启动业务、不改写测量数据。"""
import argparse
import csv
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(a, b, message):
    require(math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9), message)


def percentile(values, fraction):
    ordered = sorted(values); position = (len(ordered) - 1) * fraction
    low = int(position); high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


COMPARISONS = [
    ("原始→在线全方案", ("original", "measured"), ("online", "online-near-fastplt")),
    ("原始→当前源码/PGO", ("original", "measured"), ("online", "baseline")),
    ("当前源码/PGO→在线全方案", ("online", "baseline"), ("online", "online-near-fastplt")),
    ("当前源码/PGO→BOLT", ("online", "baseline"), ("online", "online")),
    ("BOLT→快捷PLT增量", ("online", "online"), ("online", "online-near-fastplt")),
    ("原始→离线参照", ("original", "measured"), ("offline", "measured")),
    ("基线→全回滚漂移", ("online", "baseline"), ("online", "rollback")),
    ("BOLT→撤销快捷PLT漂移", ("online", "online"), ("online", "after-fast-rollback")),
    ("在线与离线wall比", ("offline", "measured"), ("online", "online-near-fastplt")),
]


def verify(directory):
    root = Path(directory).resolve(); manifest = load(root / "manifest.json")
    require(manifest["schema"] == "tgo.vllm920b-evidence.v1", "证据schema不符")
    for name, expected in manifest["files"].items():
        path = root / name
        require(path.resolve().is_relative_to(root) and path.is_file() and not path.is_symlink(), "无效证据文件: " + name)
        require(digest(path) == expected["export_sha256"], "公开文件摘要不符: " + name)
    state = load(root / "reports/formal-host-v1-state.json")
    audit = load(root / "reports/formal-host-v1-audit.json")
    terminal = load(root / "reports/formal-host-v1-terminal-audit.json")
    matrix = load(root / "inputs/matrix.json")
    require(state["status"] == "complete" and audit["passed"] and terminal["passed"], "原审计或矩阵未通过")
    expected = [(seed, arm) for seed, order in enumerate(state["orders"]) for arm in order]
    require(len(expected) == 9 and [(s["seed"], s["arm"]) for s in state["steps"]] == expected, "正式轮次/顺序不符")
    frozen = load(root / "reports/frozen-selection-v1.json")
    require(frozen["selected"]["tag"] == state["candidate"] == "full-stack-lite-fast-r1", "冻结候选不一致")
    require(frozen["selected"]["session_sha256"] == state["candidate_session_sha256"], "冻结候选来源不一致")
    require(frozen["frozen_unix"] <= state["started_unix"], "候选在正式矩阵之后才冻结")
    host_processes = set(); output_hashes = set(); rows = {}; requests = {}; release_waits = []; fast_sites = []
    for step in state["steps"]:
        require(step["exit_code"] == 0, "正式进程失败")
        session = load(root / "sessions" / (step["tag"] + ".json"))
        require(session["status"] == "complete" and session["seed"] == step["seed"], "会话状态/seed不符")
        require(session["software_sha256"] == state["software_sha256"], "会话间软件来源不一致")
        host_processes.add((session["host_pid"], session["host_starttime"]))
        require(session["cow_normalization"]["code_bytes_unchanged"], "缺少COW原值核验")
        ready = session["ready"]; main_tid = ready["pid"]
        require(ready["starttime_ticks"] == session["host_starttime"], "进程启动身份改变")
        arm = "current" if step["arm"] == "online" else step["arm"]
        require(ready["sources"] == matrix[arm]["sources"], "业务源码来源不符")
        require(set(ready["libraries"]) == set(matrix[arm]["libraries"]), "输入库集合不符")
        for name, item in ready["libraries"].items():
            require(item["sha256"] == matrix[arm]["libraries"][name]["sha256"], "本次输入库摘要不符")
        gates = [session[k] for k in ("python_gate_baseline", "python_gate_online", "python_gate_rollback") if k in session]
        require(len(gates) == (3 if step["arm"] == "online" else 1), "语义门禁不完整")
        require(all(g["checks"] == 329 and g["digest"] == gates[0]["digest"] and g["cached_pattern_identities"] == gates[0]["cached_pattern_identities"] for g in gates), "语义/缓存身份改变")
        groups = {}
        for sample in session["results"]:
            name = sample.get("state", "measured"); key = (step["tag"], name, sample["index"])
            require(key not in requests, "重复请求")
            require(sample["tokens"] == sample["actual_worker_steps"] == 1002, "不完整请求")
            require(sample["scheduled_tokens"] == [5] + [1] * 1001, "prefill/decode调度形态改变")
            require(sample["worker_tids"] == [main_tid] and sample["llm_identity"] == ready["llm_identity"], "原主线程/LLM身份改变")
            require(sample["output_sha256"] == ready["full_sha256"], "输出摘要改变")
            require(sample["source_sha256"] == ready["sources"], "ROI业务源码改变")
            require(sample["business_end_monotonic_ns"] - sample["business_start_monotonic_ns"] == sample["wall_ns"], "wall边界不符")
            before = {t["tid"]: t for t in sample["thread_affinities_before_raw"]}
            after = {t["tid"]: t for t in sample["thread_affinities_after_raw"]}
            require(set(before) == set(after) and len(before) == 56 and main_tid in before, "ROI前后56线程集合不符")
            for snapshot in (before, after):
                require(any("release" in t["comm"].lower() for t in snapshot.values()), "没有release线程")
                for tid, thread in snapshot.items():
                    allowed = {192} if tid == main_tid else {194, 196, 198}
                    require(bool(thread["cpus"]) and set(thread["cpus"]) <= allowed, "发现整体单核或线程亲和性越界")
            scheduling = []
            for tid, thread in after.items():
                if tid == main_tid or "release" in thread["comm"].lower():
                    delta = [int(a) - int(b) for a, b in zip(thread["schedstat"].split(), before[tid]["schedstat"].split())]
                    require(len(delta) == 3 and all(x >= 0 for x in delta), "schedstat不连续")
                    scheduling.append({"main": tid == main_tid, "runtime_s": delta[0] / 1e9, "wait_s": delta[1] / 1e9})
                    if tid != main_tid: release_waits.append(delta[1] / 1e9)
            for event in sample["events"].values():
                require(event["enabled"] > 0 and event["running"] / event["enabled"] >= .999, "PMU复用或无效")
            requests[key] = {"wall_s": sample["wall_ns"] / 1e9, "scheduling": scheduling, "seed":step['seed'], "arm":step['arm']}
            output_hashes.add(sample["output_sha256"]); groups.setdefault(name, []).append(sample)
        expected_states = {"baseline", "online", "online-near-fastplt", "after-fast-rollback", "rollback"} if step["arm"] == "online" else {"measured"}
        require(set(groups) == expected_states and all(len(v) == 2 for v in groups.values()), "每状态两个请求的口径不符")
        for name, samples in groups.items():
            rows[(step["seed"], step["arm"], name)] = {
                "wall_s": statistics.median(s["wall_ns"] for s in samples) / 1e9,
                **{event: statistics.median(s["events"][event]["count"] for s in samples) for event in ("cycles", "instructions")}}
        if step["arm"] == "online":
            require([x["name"] for x in session["loaded"]] == state["libraries"], "在线库集合改变")
            entries = sum(x["function_count"] for x in session["loaded"])
            require(entries == 2576 and session["apply"]["patch_count"] == session["rollback"]["patch_count"] == entries, "入口提交/回滚不符")
            sites = session["near_fastplt"]["function_count"]; fast_sites.append(sites)
            require(session["fast_apply"]["patch_count"] == session["fast_rollback"]["patch_count"] == sites, "快捷PLT提交/回滚不符")
            require(all(session["near_fastplt"]["proof"].values()) and session["near_fastplt"]["stats"]["conflicts"] == 0, "快捷PLT证明不符")
            for library in session["loaded"]:
                build = load(root / "artifacts" / (step["tag"] + "-" + library["name"]) / "build.json")
                require(build["source_sha256"] == ready["libraries"][library["name"]]["sha256"] and build["runtime_identity"] and build["exit_code"] == 0, "BOLT构建输入/身份模式不符")
                require(ready["ready_monotonic_ns"] < build["started_monotonic_ns"] < build["finished_monotonic_ns"] < session["results"][0]["business_start_monotonic_ns"], "BOLT构建不在业务ready与正式ROI之间")
    require(len(host_processes) == 9 and len(requests) == 42 and len(output_hashes) == 1, "进程/请求数或跨臂输出不符")
    require(audit["independent_processes"] == 9 and audit["output_sha256"] == next(iter(output_hashes)), "原审计进程数/输出不符")
    with (root / "reports/formal-host-v1-requests.csv").open(newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    require(len(csv_rows) == 42, "CSV不是42个请求")
    seen = set()
    for item in csv_rows:
        key = (item["tag"], item["state"], int(item["index"]))
        require(key not in seen and key in requests, "CSV重复或缺少原始请求"); seen.add(key)
        original = requests[key]; close(float(item["wall_s"]), original["wall_s"], "CSV wall与原始ns不符")
        require(int(item['seed'])==original['seed'] and item['arm']==original['arm'], 'CSV seed/实验臂不符')
        require(int(item["thread_count"]) == 56, "CSV线程数不符")
        for prefix, main in (("main", True), ("release", False)):
            for field, stat in (("runtime_s", "runtime_s"), ("wait_s", "wait_s")):
                close(float(item[prefix + "_" + field]), sum(s[stat] for s in original["scheduling"] if s["main"] == main), "CSV schedstat与原始快照不符")
    require(len(audit['rows'])==len(rows) and len({(r['seed'],r['arm'],r['state']) for r in audit['rows']})==len(rows), '原审计状态行缺失或重复')
    for row in audit["rows"]:
        actual = rows[(row["seed"], row["arm"], row["state"])]
        for field in ("wall_s", "cycles", "instructions"): close(actual[field], row[field], "原审计的进程级中位数不符")
    comparisons = {}
    for label, reference, candidate in COMPARISONS:
        paired = [100 * (1 - rows[(seed, *candidate)]["wall_s"] / rows[(seed, *reference)]["wall_s"]) for seed in range(3)]
        boot = [statistics.median(paired[i] for i in choices) for choices in itertools.product(range(3), repeat=3)]
        comparison = {"paired_reductions_pct": paired, "median_pct": statistics.median(paired), "descriptive_bootstrap_95_pct": [percentile(boot, .025), percentile(boot, .975)], "independent_pairs": 3}
        recorded = audit["comparisons"][label]
        require(recorded["independent_pairs"] == 3, "配对数量不符")
        require(len(recorded['paired_reductions_pct'])==3 and len(recorded['descriptive_bootstrap_95_pct'])==2, '配对/区间记录不完整')
        close(comparison["median_pct"], recorded["median_pct"], "配对中位收益不符")
        for field in ("paired_reductions_pct", "descriptive_bootstrap_95_pct"):
            for a, b in zip(comparison[field], recorded[field]): close(a, b, "逐对收益/描述性区间不符")
        comparisons[label] = comparison
    topology = terminal["cpu_topology"]
    for cpu in (194, 196, 198):
        require((topology[str(cpu)]["physical_package_id"], topology[str(cpu)]["core_id"]) != (topology["192"]["physical_package_id"], topology["192"]["core_id"]), "主辅CPU共享物理核心")
    coverage = load(root / "reports/max-coverage-seven-v2-audit.json")
    require(coverage["passed"] and coverage["patched_entries"] == 142748 and len(coverage["libraries"]) == 7, "七库覆盖记录不符")
    return {"schema": "tgo.vllm920b-verification.v1", "passed": True, "independent_processes": 9,
            "requests": 42, "thread_count_per_roi": 56, "main_cpu": 192, "auxiliary_cpus": [194, 196, 198],
            "separate_physical_cores": True, "release_runqueue_wait_range_s": [min(release_waits), max(release_waits)],
            "performance_entries": 2576, "fast_plt_sites": fast_sites, "seven_library_entries": 142748,
            "comparisons": comparisons, "brbe_collected": False,
            "scope": "原生实测设备桩完整generate与最终同步的主机wall；不是合成数据或真实NPU计算吞吐"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=str(Path(__file__).resolve().parents[1] / "evidence/vllm-920b-20261106"))
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.directory), indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, KeyError, OSError, TypeError) as error:
        parser.exit(1, "核验失败: " + str(error) + "\n")


if __name__ == "__main__": main()
