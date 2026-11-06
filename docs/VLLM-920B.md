# 920B vLLM 算子下发实测证据

2026-11-06正式矩阵`formal-host-v1`的数据已纳入仓库：[数据说明与完整文件](../evidence/vllm-920b-20261106/README.md)、[逐请求CSV](../evidence/vllm-920b-20261106/reports/formal-host-v1-requests.csv)、[完整报告](../evidence/vllm-920b-20261106/REPORT.md)。这是920B原生运行的真实计时数据。

| 比较 | 三轮配对wall缩短中位数 |
| --- | ---: |
| 登记原始组→当前源码/PGO＋在线BOLT＋快捷PLT | **41.117%** |
| 固定当前源码/PGO→在线BOLT＋快捷PLT | **21.022%** |
| 固定当前源码/PGO→在线BOLT | **11.634%** |
| 在线BOLT→快捷PLT增量 | 10.116% |
| 登记原始组→离线参照 | 45.206% |

每轮每状态先取两个请求wall的中位数，再同seed配对计算缩短比例，最终取三个配对百分比的中位数。不同分母的收益不可相加；41.117%包括源码/编译优化，不能全部归给BOLT。原始组和全方案的三个进程级wall中位数分别为27.5447秒和16.0074秒。

负载为Qwen3-1.7B dummy BF16、TP1、batch1、纯eager、任务队列开启、设备桩。每个请求计入完整generate及最终同步：5-token prefill、1001次decode、输出1002 token。预先50-token settle不计入测量。指标是主机算子下发路径的wall，不代表真实NPU计算吞吐，也没有使用BRBE采集。

三轮共9个独立进程、42个请求，未剔除样本；候选在正式矩阵前冻结。公开JSON保留每个请求的原始ns、主线程PMU、调度形态、输出与对象身份、两级回滚、逐线程亲和性和schedstat。主线程CPU192，release/ACL等辅助线程194/196/198；42个ROI前后均为56个线程，主辅CPU不共享物理核心。release累计runqueue等待逐请求范围0.00220044～0.01950227秒，不能单凭此指标判定每次自旋原因。

性能版实际六库2576热点入口；三轮快捷PLT位点2514/2722/2683。另有七库全量142748入口的覆盖验证，和性能矩阵分开报告；Transformer未在线装载，不计八库成功。

从仓库根目录可独立复算，无需模型、NPU或BOLT：

```sh
python3 scripts/verify_vllm_920b_evidence.py
```

导出时只脱敏路径等文本，所有JSON测量数值保持原值，CSV逐字节保留。`manifest.json`同时记录原文件和公开文件的摘要；大ELF、完整trace及原实验目录留在内网。数据来自独立冻结实验，保留原基线与优化栈口径；当前通用工具和BRBE后端的硬件收益需另行验证。

已有[2026-10-29离线39.47%结果](../evidence/2026-10-29/vllm-full-route/README.md)使用Qwen3.5-0.8B和短decode ROI，模型、ROI和统计轮次均不同。两份实测数据分别保留。
