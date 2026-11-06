#!/usr/bin/env python3
"""从只读冻结工程导出vLLM实测证据；路径脱敏，测量数值不变。"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from verify_vllm_920b_evidence import verify


def digest(data):
    return hashlib.sha256(data).hexdigest()


def numbers(value):
    if isinstance(value, dict):
        return [n for item in value.values() for n in numbers(item)]
    if isinstance(value, list):
        return [n for item in value for n in numbers(item)]
    return [(type(value).__name__, value)] if isinstance(value, (int, float, bool)) else []


README = """# 920B vLLM 算子下发优化实测数据

2026-11-06，920B-47，正式矩阵 `formal-host-v1`。这是原生运行得到的设备桩主机数据，不是合成夹具，也不是BRBE采集结果。

| 比较 | 三轮配对 wall 缩短中位数 |
| --- | ---: |
| 登记原始组 → 源码/PGO＋在线BOLT＋快捷PLT | **41.117%** |
| 登记原始组 → 当前源码/PGO | 25.933% |
| 固定当前源码/PGO → 在线BOLT＋快捷PLT | **21.022%** |
| 固定当前源码/PGO → 在线BOLT | 11.634% |
| 在线BOLT → 快捷PLT增量 | 10.116% |
| 登记原始组 → 离线参照 | 45.206% |

每轮每状态先取两个完整请求wall的中位数，再以同seed配对计算 `100 × (1 − candidate / baseline)`，最后取三个配对百分比的中位数。各行分母不同，不能直接相加。原始组和全方案的进程级wall中位数分别为27.5447秒和16.0074秒；这两个总体中位数直接相除不等于上述逐对中位统计。

负载为Qwen3-1.7B dummy BF16、TP1、batch1、纯eager、任务队列开启、设备桩。每个ROI涵盖完整generate和最后同步：5个prompt token的prefill、1001次decode、输出1002个token。每次ROI之前的50-token settle不计入wall。结论限于主机算子下发路径，不外推真实NPU计算吞吐。主线程PMU是辅证，收益采用主机wall。

正式数据包含**三轮、9个独立进程、42个请求**，全部保留。三轮进程顺序分别为原始→在线→离线、离线→在线→原始、在线→原始→离线。在线进程的基线、BOLT、BOLT＋快捷PLT、撤销快捷PLT、全部入口回滚各测两次。候选在正式矩阵前冻结，不用这42个请求重新选型。只有三个独立配对，bootstrap区间仅作描述。

## CPU亲和性与release线程

正式42个ROI逐一保存前后原样线程快照，每个56线程。主线程CPU192，release/ACL等辅助线程允许194/196/198，NUMA2；主辅CPU不共享物理核心。没有在ROI边界复绑覆盖快照，也没有把这批完整业务整体绑定单核。

release逐请求累计runqueue等待为0.00220044～0.01950227秒。它是运行队列等待时间，不能单凭此指标推断每次主线程自旋或下发停顿的原因。控制器200–207、BOLT208–215是原实验配置。上述分核结论对应本矩阵，不用于追溯认证其它历史测试。

## 文件与复算

- [完整原报告](REPORT.md)：分母、逐轮wall、描述性区间、构建/停车成本、算法、边界和失败说明。
- [逐请求CSV](reports/formal-host-v1-requests.csv)：原文件逐字节复制，42行，含wall、主线程/release运行和等待时间。
- [正式审计](reports/formal-host-v1-audit.json)：原统计、主线程PMU、逐请求调度、覆盖和成本。
- `sessions/formal-host-v1-r*.json`：9个会话的请求级原始ns、线程快照、schedstat、PMU、输出/对象身份和回滚记录；JSON只对路径等文本脱敏，全部数值保持原值。
- [矩阵顺序](reports/formal-host-v1-state.json)、[候选冻结](reports/frozen-selection-v1.json)、[输入](inputs/matrix.json)、[画像来源](inputs/profile-provenance.json)。
- `artifacts/*/build.json`：正式三轮六库共18份BOLT命令、输入/画像/工具SHA和构建时间元数据，不包含ELF。
- [七库覆盖审计](reports/max-coverage-seven-v2-audit.json)及`sessions/`中的其它会话：额外的选型/覆盖验证，不参与正式42个请求的性能统计。性能版六库2576入口；七库全量142748入口。Transformer生成过但未在线装载，不记为八库成功。
- `source-snapshots/`：ROI边界、矩阵调度和原审计的历史源码参考，用于阅读；不是当前工具运行入口，也不表示原实验依赖已经完整打包。
- [manifest](manifest.json)：每个原文件和导出文件各自的SHA/大小、路径转换说明。原文件SHA用于来源追溯；导出SHA用于公开数据完整性，不是应用优化准入名单。

从仓库根目录只读复算，不需要模型、NPU、BOLT或perf：

```sh
python3 scripts/verify_vllm_920b_evidence.py
```

校验包含42请求CSV/ns的一致性、统计分母、语义摘要、线程分核、release等待、PMU有效性、候选冻结、在线入口/快捷PLT回滚和18份构建元数据。不重跑原地的ELF/trace/源码二进制SHA审计；该历史审计结果随包保存，原始大工件仍留在内网。

既有2026-10-29短decode ROI、Qwen3.5-0.8B离线39.47%数据仍在仓库`evidence/2026-10-29/vllm-full-route/`，模型与ROI均不同，不能与本矩阵合并。这批41.117%也不能改称新BRBE后端或通用工具在所有未来版本上的收益。
"""


def export(source, output):
    source = Path(source).resolve(); output = Path(output).resolve()
    if source == output or source in output.parents:
        raise ValueError("导出必须写到冻结工程之外")
    output.mkdir(parents=True, exist_ok=False)
    mapping = {"REPORT.md": "REPORT.md"}
    reports = ["formal-host-v1-audit.json", "formal-host-v1-requests.csv", "formal-host-v1-state.json",
               "formal-host-v1-terminal-audit.json", "frozen-selection-v1.json", "max-coverage-seven-v2-audit.json",
               "max-coverage-seven-v2-state.json", "max-coverage-seven-v1-state.json", "compiler-source-final-audit.json"]
    for name in reports: mapping["reports/" + name] = "reports/" + name
    for name in ("matrix.json", "actual-input-hashes.json", "profile-provenance.json"):
        mapping["inputs/" + name] = "inputs/" + name
    state = json.loads((source / "reports/formal-host-v1-state.json").read_text())
    if state["status"] != "complete": raise ValueError("原矩阵未完成")
    for step in state["steps"]:
        mapping["sessions/" + step["tag"] + ".json"] = "runs/" + step["tag"] + "/session.json"
        if step["arm"] == "online":
            for name in state["libraries"]:
                relative = "artifacts/" + step["tag"] + "-" + name + "/build.json"
                mapping[relative] = relative
    for tag in ("full-stack-lite-fast-r1", "max-coverage-seven-v2", "full-stack-v4-r4", "python-private-r2", "combined-eval-fast-r5"):
        mapping["sessions/" + tag + ".json"] = "runs/" + tag + "/session.json"
    for name in ("scripts/model_worker.py", "scripts/formal_matrix.py", "analysis/audit_formal.py", "analysis/render_report.py"):
        mapping["source-snapshots/" + name] = name

    def text(value):
        value = value.replace(str(source), "${FROZEN_ROOT}")
        value = re.sub(r"/home/[^/\s]+/projects/vllm/" + re.escape(source.name), "${FROZEN_ROOT}", value)
        value = re.sub(r"/home/[^/\s]+/projects/trace-guided-optimization", "${TGO_HISTORY_ROOT}", value)
        value = re.sub(r"/home/[^/\s]+/projects/vllm", "${VLLM_HISTORY_ROOT}", value)
        value = re.sub(r"/home/[^/\s]+", "${USER_HOME}", value)
        return re.sub(r"\b(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b", "${PRIVATE_HOST}", value)

    def sanitize(value):
        if isinstance(value, str): return text(value)
        if isinstance(value, list): return [sanitize(x) for x in value]
        if isinstance(value, dict):
            result = {text(k): sanitize(v) for k, v in value.items()}
            if len(result) != len(value): raise ValueError("路径脱敏造成字典键冲突")
            return result
        return value

    records = {}
    for target, relative in sorted(mapping.items()):
        original_path = source / relative; raw = original_path.read_bytes()
        if relative in state["software_sha256"] and digest(raw) != state["software_sha256"][relative]:
            raise ValueError("原冻结源码摘要不符: " + relative)
        destination = output / target; destination.parent.mkdir(parents=True, exist_ok=True)
        if original_path.suffix == ".json":
            value = json.loads(raw); cleaned = sanitize(value)
            if numbers(value) != numbers(cleaned): raise ValueError("脱敏改变了数值: " + relative)
            payload = (json.dumps(cleaned, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        elif original_path.suffix == ".csv": payload = raw
        else: payload = text(raw.decode()).encode()
        destination.write_bytes(payload)
        if digest(original_path.read_bytes()) != digest(raw): raise ValueError("导出期间原文件变化: " + relative)
        records[target] = {"source_relative": relative, "source_sha256": digest(raw), "source_bytes": len(raw),
                           "export_sha256": digest(payload), "export_bytes": len(payload),
                           "transformation": "逐字节复制" if original_path.suffix == ".csv" else "路径文本脱敏；JSON所有数值保持原值"}
    (output / "README.md").write_text(README, encoding="utf-8")
    records["README.md"] = {"export_sha256": digest(README.encode()), "export_bytes": len(README.encode()), "transformation": "新增数据说明"}
    manifest = {"schema": "tgo.vllm920b-evidence.v1", "measurement_date": "2026-11-06", "matrix": "formal-host-v1",
                "real_measurement": True, "brbe_collected": False, "numeric_values_unchanged": True,
                "redaction": "本地用户名/绝对工程路径与私网IP替换为变量；保留所有测量数值和原/公开两种摘要；不复制ELF、完整trace或凭据",
                "files": records}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return verify(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(export(args.source, args.output), ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(1, "导出/核验失败，已保留输出: " + str(error) + "\n")


if __name__ == "__main__": main()
