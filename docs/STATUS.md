# 当前工作状态（2026-10-29）

Goal 保持 active。当前正在从 alpha 推进到 0.2.0：Go compiler 策略已实现，尚无
稳定 Go/vLLM 收益；新 AppImage 正在构建验证，未宣称所有负载适配完成。

主仓库在 `920B-47:~/projects/trace-guided-optimization`，本地同名目录通过 Git 推送
到远端工作区。没有修改 a3 网络、既有实验源码或原负载 ELF。

已实现：

- `record / normalize / optimize / tune / validate / bind / inspect / doctor`。
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

当前分发文件位于远端 `dist/alpha-0.1.0/tgo-0.1.0-aarch64.AppImage`，约 91 MiB。
它携带自建工具和 Python，已验证无 FUSE 启动及完整调优流程。生成的入口随后可
独立执行，原解释器入口保持原状。源码版本和实际依赖在包内 `package-manifest.json`。

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
| Go runtime 函数布局 | `runs/go-compiler-arm-layout-runtime-r1` | 正在筛选 |
| vLLM retired-branch SPE | `runs/vllm-spe-retired-r1` | 新 ROI 画像已采集，等待 recovery 工具复测 |

最终 alpha 还完成了 `runs/appimage-alpha-r1`：从 PATH 移除编译器和系统 Python，
只保留基本 shell 工具，用分发文件完成端到端调优。之后在项目外的 `/tmp` 执行
生成入口，确认 ROI helper 来自 bundle，虚拟环境路径仍保持正确。

后续优先任务：

1. 完成 Go runtime 布局筛选与 0.2.0 AppImage 无 C 编译器环境验证。Go 实现已通过
   本地和 ARM 的集成测试；未把编译成功或局部正收益当作独立确认结果。
2. vLLM 获益与泛化。比较紧凑布局，处理原 Python 缺少重定位和热 DSO 缺少符号的
   能力边界；可参考旧 BPU 的恢复/重编译经验，但须独立验证，不默认假设代码页安全。
3. 自动后端/ROI 策略，更多 DSO 组合与失败回退，跨进程和混合 prefill/decode 适配。
4. 跨机器 bundle 重绑定、较低 glibc 构建基线、x86 完整 BOLT/AppImage 实测与冷 SDK CI。

不要重复创建 Goal。下一轮先读取当前 `get_goal` 和此状态，再继续尚未完成的工作。
