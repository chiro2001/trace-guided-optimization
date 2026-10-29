# 当前工作状态（2026-10-29）

本轮最高优先级已完成：**0.5.0 AppImage 从头自动复现 vLLM 约 40% 收益**。
固定 CPU-stub 镜像、Qwen3.5-0.8B、纯 eager 四步 decode、1 次 warmup、9 次测量请求。
关闭容器网络，不传画像或构建复用参数，完成 SPE 发现、GCC ROI PGO/LTO、Python
原生 BOLT、七库完整 QEMU、八库重写、三对初筛和另外五对独立确认。

- 从头 AppImage：ROI 时间减少中位数 **39.47%**，范围 **36.80%～42.96%**；
  cycles 减少 **40.80%**，指令减少 13.18%，前端 stall 减少 63.69%，L1I read miss
  减少 52.81%。五对均正确并实际加载八库，全部 PMU 无复用。
- 本轮新构建/画像缓存复用：独立五对 ROI 减少 **39.29%**，cycles 减少 **40.66%**。
  未导入旧冻结优化库。旧八库经新入口的 38.97% 另列为加载回归。
- 两种新路线都在新容器完成相邻 `python3.tgo` 检查：只读 bundle、无源码或工具
  挂载、`/tmp` 工作目录、bundle 内 ROI helper、八库加载、关闭优化恢复原库及输出一致。
- 新流程三轮 GCC 训练得到 149 份 gcda、27 个 ROI，清理 285 份构建遗留画像；
  三轮原生 BOLT 训练得到 27 份 fdata。QEMU 记录一个完整 ROI、352,496,354 条指令，
  未截断；负载仍执行九个 ROI，完整输出与发现阶段一致。
- 本地 47 项测试中 45 项通过、2 项 LLVM/QEMU SDK 测试跳过；ARM 43 项通过、
  4 项 Go SDK 测试跳过。真实 ARM QEMU、LLVM 合并与包内资源检查通过。

分发文件：`dist/0.5.0/tgo-0.5.0-aarch64.AppImage`，约 141 MiB。
SHA256：`21719e548822588e6d99d3f346072df66c8d8a5c47fd58a4494b3ac6d447cce0`。
包内运行时代码提交 `6b890bb`，构建时源码干净，源码与 native runtime 随包携带。
AppRun 自检会检查编译输入的位置和 SHA。首版漏导出 TGO_APPDIR 的失败工件保留于
`dist/0.5.0-asset-export-failed`，失败记录在 `runs/vllm-appimage-full-v05-r1`。

从头运行：`runs/vllm-appimage-full-v05-r2`；入口：该目录的 `run`（executable 模式）。
新容器检查：`runs/vllm-appimage-entry-v05-r2`。
缓存复用模式：`runs/vllm-auto-hybrid-r1`；其独立入口检查：`runs/vllm-hybrid-entry-smoke-v05-r1`。
完整小型证据见 [vLLM 完整路线](../evidence/2026-10-29/vllm-full-route/README.md)，
复现命令见 [负载协议](WORKLOADS.md)。

先前约 5% 的路线未补齐源码 PGO/LTO、Python 原生 BOLT 和完整八库策略；NumPy 的
非零 entry 也曾造成误判。新的混合画像按输入 SHA 逐对象绑定，选库权重统一来自
基线发现，不相加原生边计数、QEMU 计数和 SPE 采样单位。高风险恢复假设只用于
完整指纹匹配的七库，其他版本仍走通用保守策略。

收益解释为该 CPU-stub 主机侧 decode 路径，不外推真实 NPU 吞吐。通用工具仍有
边界：Go 编译负载尚无稳定收益；bundle 默认绑定绝对路径；CPython 重建需要目标
GCC/g++/make。原入口、旧实验与 STOP 未修改，所有失败与原始证据保留。

以下保留 0.4.0 及更早阶段记录：

0.4.0 增加默认三轮训练、不可变画像集合及独立 fdata 合并，
本地 33 项测试中一项真实 LLVM 合并测试因工具缺失跳过，ARM 33 项全部通过。
最终包在普通用户环境自动选择 perf，合成负载三轮采集后独立五对确认中位数减少
60.54%；计数和项目外独立入口均验证。Go 三轮 profile/无 CC smoke 通过，仍不作为
稳定性能收益证据。vLLM 单画像完整布局确认 4.53%，显式混合画像确认 4.31%；
同配置三轮训练完整布局选择 compact，独立五对确认 5.67%（2.91%～7.57%）。

0.3.0 已完成自动后端/选库/子集回退和分发验证。复用已验证 ROI
画像时，自动选择 vLLM 三库并独立五对确认，decode ROI 中位数减少 4.74%；
从头采集的另一份画像未找到稳定收益并保留原版。Go 尚无稳定收益。

主仓库在 `920B-47:~/projects/trace-guided-optimization`，本地同名目录通过 Git 推送
到远端工作区。没有修改 a3 网络、既有实验源码或原负载 ELF。

已实现：

- `record / normalize / merge / optimize / tune / validate / bind / inspect / doctor`。
- perf 整段、时间窗口、attach、FIFO marker；SPE；QEMU 聚合路径与显式 marker。
- Python 函数 hook、原生 C ROI API、vLLM 四步 eager decode adapter。
- 热点与 ELF 身份绑定、BOLT、多布局筛选、独立确认、实际映射和输出验证。
- 原生 ELF 入口，支持完整命令和可替代解释器两种模式；绝对路径 DSO 重定向、
  同名库区分、虚拟环境和 `$ORIGIN` 处理、源文件漂移检查、关闭优化。
- 自建 LLVM/BOLT 22.1.0、QEMU 10.0.0、perf 6.14、CPython 3.12.13、patchelf，
  以及私有 GLib、libelf 等 SDK 依赖；单个 AArch64 AppImage。
- perf SPE 的 dlfilter 兼容桥：源/目标地址来自解码器，指令字来自 mmap 感知的
  object-code API。缺失的预测位明确保持未知。
- Go compiler CPU profile、原生 toolexec、PGO/紧凑内联/对齐/混合画像、包内函数布局，
  以及可选 runtime Go 函数布局；保持 Go linker 生成全部 PC/栈/GC metadata。
  验证记录实际 compiler 执行、独立 cache、预热、输出和最终函数地址；版本漂移及
  Go build cache 身份均被检查。详见 `docs/GO-COMPILER.md`。
- 可选 AArch64 recovery BOLT：完整补丁已纳入仓库，使用独立源码/构建/安装目录，
  通过 `bootstrap.py --components llvm --llvm-variant recover` 自动构建。
- 实测事件权限后优先 SPE，其次 perf，再其次 QEMU；按每库元数据自动选择 BOLT
  策略。单库失败保留原版；整组无收益或输出失败时探测子集并复用已生成对象。
  初筛按中位数排序，独立确认仍要求逐对超过门槛。每轮原始证据分别保留。
- TGOCFG02 入口包含 SHA256：元数据变化时验证内容，兼容同镜像新容器的 overlay
  设备号/inode 变化。Go helper 同步支持内容身份，避免逐 compiler 重复哈希。
- 默认三轮同配置训练。各轮独立处理 ASLR/mmap 后，使用真实 merge-fdata 累加计数；
  源证据、采样单位、ROI 时间边界及 ELF 内容均检查，嵌套集合展开并拒绝重复来源。
  Go 每轮独立冷 cache，通过 pprof 合并。已有 --run 不重新采集。
- 硬件探测使用实际 recorder/SPE AUX 缓冲配置，避免小探测成功后因 mlock 权限
  在真正采集时失败；cycles recorder 和探测统一固定 128 页 ring。
  perf/SPE 禁止写全局 build-id cache，避免实测容器重复采集出现 `/ (deleted)`。
  仍在原始文件中保留 build-id；多数样本无法对应可读 ELF 时保存诊断并拒绝继续。

当前分发文件位于远端 `dist/0.4.0/tgo-0.4.0-aarch64.AppImage`，约 115 MiB。
它携带自建工具和 Python，已验证无 FUSE 启动及完整调优流程。生成的入口随后可
独立执行，原解释器入口保持原状。源码版本和实际依赖在包内 `package-manifest.json`。
SHA256：`053d4472bb6c9bd6c497c5153f6420d63967e8afaf210e4082afaf4f027fe9ea`。
运行时代码提交 `c1cf4f4`，包内有全部构建输入的 SHA256，并在构建期间检查源文件漂移。
旧 alpha、0.2.0、0.3.0 均保留；0.3.0 最终包 SHA256 为
`a766757f7f31c38358355c068da7eaf762825c71c860ba0e947f273a9d0bed98`。

原始运行目录、失败证据和构建日志留在远端 `runs/`、`.tgo-cache/`、`deps/`，
不纳入 Git。小型验证 JSON 放入仓库 `evidence/2026-10-29/`。

| 实验 | 远端目录 | 状态 |
| --- | --- | --- |
| 自建 BOLT 的原生取指压力合成负载 | `runs/native-selfbuilt-r1` | 独立 5 对确认获益 |
| 新构建 CPython 前端 microbench | `runs/python-fresh-tune-r1` | 独立 5 对确认获益 |
| SPE 分支转换和原生候选 | `runs/native-spe-bolt-r4` | 36,103 条全部校验，3 对通过 |
| 单文件 AppImage 一条命令调优 | `runs/appimage-e2e-r1` | 相邻 Python ELF 入口已实际执行 |
| vLLM IP 采样 / libpython 候选 | `runs/vllm-bolt-r1` | 正确性通过，性能回退，不推荐 |
| vLLM SPE / libpython 单候选 | `runs/vllm-spe-bolt-r1` | 正确性通过，性能回退，不推荐 |
| vLLM SPE 多布局搜索 | `runs/vllm-spe-tune-r1` | 三种布局未通过收益门槛，自动选择原版 |
| vLLM QEMU | `runs/vllm-qemu-r1` | 600 秒内未进入 ROI，超时退出 |
| Go PGO / 紧凑内联 / 对齐 | `runs/go-compiler-arm-r2` | 均正确，三对筛选无稳定收益，保留原版 |
| Go 包内函数布局 | `runs/go-compiler-arm-layout-r1` | 最终 ELF 地址与语义检查通过，无收益 |
| Go runtime 函数布局 | `runs/go-compiler-arm-layout-runtime-r2` | 移动 1,357 个 runtime 符号；中位数 -1.04%，保留原版 |
| vLLM retired-branch SPE | `runs/vllm-spe-retired-r1` | ROI 画像已采集 |
| vLLM 三库 recovery | `runs/vllm-spe-retired-multi-r1` | 独立五对确认 3.68%，范围 2.50%～6.41% |
| Go AppImage 功能 smoke | `runs/appimage-go-smoke-0.2.0` | 无 C 编译器、显式 SDK、项目外入口均通过 |
| 自动选库 / vLLM 画像复用 | `runs/vllm-auto-tune-r1` | 自动三库 recovery；独立五对 4.74%，2.92%～7.56% |
| AppImage 从头采集 vLLM | `runs/vllm-appimage-auto-r1` | 自动 SPE、三库及六个子集；无稳定收益，保留原版 |
| AppImage 自动采集 / 原生前端 | `runs/appimage-native-auto-r1` | 自动 SPE 和三种布局；独立五对 56.55% |
| 最终内容身份入口 / 原生前端 | `runs/appimage-native-abi2-r1` | 独立五对 56.85%，56.01%～58.71% |
| 最终发布包 / 原生前端 | `runs/appimage-native-0.3.0-final` | 独立五对 57.38%，56.43%～58.71% |
| 最终 Go AppImage smoke | `runs/appimage-go-smoke-0.3.0-final` | 无 C 编译器及项目外入口通过 |
| 0.4.0 初始三轮原生合成负载 | `runs/appimage-native-0.4.0-final` | 自动 QEMU；独立五对 57.76%，54.88%～59.43%；计数和独立入口通过 |
| 0.4.0 最终三轮原生合成负载 | `runs/appimage-native-0.4.0` | 普通权限自动 perf；独立五对 60.54%，56.47%～61.34%；计数和独立入口通过 |
| 0.4.0 最终三轮 Go smoke | `runs/appimage-go-smoke-0.4.0` | 无 CC、三轮独立 cache、profile 合并和项目外入口通过 |
| vLLM 新单画像完整布局 | `runs/vllm-single-full-0.4.0-r1` | compact 独立五对 4.53%，0.89%～6.65%；新容器相邻入口通过 |
| vLLM 显式混合旧/新画像 | `runs/vllm-mixed-full-0.4.0-r1` | ext-tsp 独立五对 4.31%，1.42%～5.60%；CPU216/224 训练、224 回放 |
| vLLM 同配置三轮训练 | `runs/vllm-three-valid-0.4.0-r1` | 自动三库、完整布局、compact 独立五对 5.67%，2.91%～7.57%；三库计数逐条相加通过 |

最终 alpha 还完成了 `runs/appimage-alpha-r1`：从 PATH 移除编译器和系统 Python，
只保留基本 shell 工具，用分发文件完成端到端调优。之后在项目外的 `/tmp` 执行
生成入口，确认 ROI helper 来自 bundle，虚拟环境路径仍保持正确。

0.2.0 额外验证 Go `layout-runtime`：从 PATH 移除 C 编译器和 Go，仅通过 `--go`
指定 SDK，完成 CPU profile、重建与真实执行检查。AppImage 退出后，在项目外的
临时目录执行候选入口，确认实际运行 bundle 内 compiler。该 smoke 只证明功能，
不作为收益证据。本地及 ARM 均 11 项测试全部通过。

Go runtime r1 曾因整包中存在特殊 text 符号而跳过 runtime，不能用作 runtime 布局
的性能结论。r2 改为固定特殊符号位置、只调整普通 Go STEXT，并强制检查 runtime
匹配与最终 ELF 地址；原 r1 证据保留。

0.3.0 本地及 ARM 各 22 项测试通过，包括错误 DSO 的真实加载隔离、源漂移拒绝、
每轮证据保留、超时后代进程清理，以及原生 SHA256 与标准实现的边界向量比较。
相邻入口已在新容器中挂载到 `/usr/local/python3.12.13/bin/python3.tgo`，实际加载
已确认的三库并通过 token 校验；测试从 `/tmp` 运行、bundle 只读、未调用 AppImage。
对应新入口为 `runs/vllm-auto-tune-r1/portable-entry/python3.tgo`，其优化 DSO 未改变。
小型证据集中在 `evidence/2026-10-29/auto-0.3.0/`。

后续可继续推进：

1. 将完整路线推广至更多库版本及其他 Python 前端负载，完善自动预算、阶段进度与缓存复用。
2. 寻找 Go 编译负载的稳定收益；补充通用子进程 ELF、静态 PIE 和跨路径 bundle。
3. 更低 glibc 构建基线、x86 完整 AppImage 实测与冷 SDK CI。

下一轮先读取 `get_goal` 和此状态，按用户新目标继续。
