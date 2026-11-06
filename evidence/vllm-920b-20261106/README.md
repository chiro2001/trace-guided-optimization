# 920B vLLM 算子下发优化实测数据

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
