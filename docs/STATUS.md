# 当前工作状态（2026-10-29）

**当前最高优先级：用新采集画像、新编译产物和 TGO 自动流程复现 vLLM 八库约 40% 收益。**
原报告的 39.14% ROI 时间减少已经核对。导入旧八库通过 TGO 新入口，独立五对仍有
38.97% 中位数收益，但这只证明加载链路有效，不作为自动重建验收。

本轮已补齐内容指纹绑定的八库策略、七库完整 recovery/BIND_NOW/清理/PLT 配方、
CPython ROI GCC PGO/LTO，以及 Python 原生 BOLT ROI 计数。新建 PGO/LTO 配合 SPE
画像目前独立五对确认 21.11%；完整二进制策略配 SPE 为 14.96%（该次只优化七库）。
此前 0.4.0 的约 5% 结果遗漏了源码 PGO/LTO 和完整八库路线，不能代表旧方案上限。

新 QEMU 已成功记录一个完整四步 decode ROI：353,703,301 条动态指令；七库热点
计数与旧画像接近。Python 原生 BOLT + 七库 QEMU 的自动混合流程已完成独立五对确认：
ROI 时间减少中位数 **39.29%**（36.42%～40.78%）、cycles 减少 **40.66%**，
指令减少 13.18%、前端 stall 减少 63.53%、L1I read miss 减少 52.24%。
五次候选均实际加载全部八库，输出一致，PMU 无复用。
该次复用的仅为本轮新建 PGO/LTO 产物与本轮新采 QEMU 证据，没有导入旧优化库。
混合流程按对象绑定画像，选库权重来自基线发现，不相加不同来源的计数单位。
QEMU 可只记录第一个 ROI 窗口，负载其余请求继续执行并参加正确性校验。

当前源码测试：本地 47 项，45 项通过、2 项 SDK 测试跳过；ARM 47 项，43 项通过、
4 项 Go SDK 测试跳过。真实 QEMU 窗口和 LLVM 计数合并测试在 ARM 均通过。
这些源码改动不在旧 0.4.0 分发包中。0.5.0 已构建，约 141 MiB，源码提交
`6b890bb`，SHA256 为 `21719e548822588e6d99d3f346072df66c8d8a5c47fd58a4494b3ac6d447cce0`。
AppRun 的包内源码/runtime 路径及 SHA 自检通过，正在 `runs/vllm-appimage-full-v05-r2`
进行关闭网络、无画像/构建复用的从头验证。首版包因漏导出 TGO_APPDIR 在编译前失败，
已修复；失败包保留在 `dist/0.5.0-asset-export-failed`，r1 原始证据保留。


目标继续推进，尚未完成。0.4.0 增加默认三轮训练、不可变画像集合及独立 fdata 合并，
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

后续优先任务：

1. 新画像与新构建复用模式已确认约 40% 收益；下一步使用 0.5.0 AppImage
   从头完成发现、编译、各阶段训练和八库独立确认，随后验证新容器的独立入口。
2. 保存画像、构建身份、逐对计数、实际八库加载和新容器独立入口证据；同步 GitHub。
3. 八库路线可靠复现后，再推进 Go 编译负载稳定收益、通用子进程 ELF adapter、
   跨路径 bundle、较低 glibc 基线及 x86 分发验证。

不要重复创建 Goal。下一轮先读取当前 `get_goal` 和此状态，再继续尚未完成的工作。
