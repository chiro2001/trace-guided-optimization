# 架构与边界

管线：`WorkloadSpec → Collector → Profile → Eligibility/Policy → BOLT → Bundle → Validation → Selection`。
命令接口、采集、画像归一化、优化和部署分离，便于增加新的 trace 后端或优化器。

```mermaid
flowchart LR
  A[argv / cwd / env / ROI] --> B{采集后端}
  B --> P[perf / SPE]
  B --> Q[QEMU TCG 插件]
  P --> C[ELF 身份与画像]
  Q --> C
  C --> D[资格检查与 BOLT 策略]
  D --> E[不可变 DSO 目录]
  E --> F[新进程 AB/BA 与真实加载验证]
  F --> G[独立确认后选择布局]
  G --> H[相邻原生 ELF 入口]
```

## 数据契约

- `run.json`：argv、cwd、显式 env、ROI、后端命令、进程状态。
- `profile.json`：按完整路径和 SHA256 标识的 ELF，架构、build-id、符号/重定位、
  计数权重、原始画像位置和采集范围/质量。
- `build.json`：绑定画像与 BOLT 的 SHA，逐库命令、日志、输入/输出身份、入口、
  被跳过对象及原因。构建成功不等于有收益。
- `validation.json`：逐对正确性、真实映射、ROI/整进程时间、可用 PMU 原始值。
- `tune.json`：筛选候选与独立确认，最终可明确选择 baseline。

原始证据与中间产物保留在独立目录。目录创建拒绝复用，入口发布拒绝覆盖，失败
构建可审查，源库从不原地覆盖。CLI 可以组合到 CI 或外部调度器中；当前没有常驻
daemon，也不对已经在执行的机器码进行热补丁。在线指在线采集/attach，产物供后续启动。

## 画像正确性

QEMU 使用 per-vCPU 状态，避免把线程切换拼成分支；同线程标记决定 ROI。
未进入 marker ROI 时，通过 scoreboard 条件回调避免每块执行进入 C 回调。
连续的 QEMU TB 按真实后继 PC 合并成落空范围，非连续后继构成观察到的分支。
每个模块的地址按 loader load bias 转回 ELF VA，通过可执行 PT_LOAD 检查归属，
跨库分支采用 BOLT 的未知端点约定。统计按进程文件保存，不混合不同 ASLR 布局。
指令上限触发截断标记，默认拒绝继续；显式接受时仍保留质量字段。

`la_objopen` 的逻辑路径不总是物理路径，因此用动态表所属的 `/proc/self/maps`
映射确定真实加载文件。卸载后在同一地址重新加载不同模块会导致画像歧义，当前拒绝；
需要短 ROI 或采用带 mmap 生命周期的 perf。QEMU native exec 子进程不在本后端
范围，编译器进程树优先 perf。静态 PIE 的 QEMU 主模块 bias 尚未实现。

perf 原始文件保留 mmap/build-id 生命周期信息，由 perf2bolt 聚合。图片发现来自
loader、perf buildid-list/report，attach 可补充 `/proc/PID/maps`。ELF 身份不符拒绝。
SPE 和采样画像是统计数据，不宣称具有完整动态路径或无丢样的精度。

## 优化政策

有重定位时启用函数重排；没有重定位时默认保留函数位置，仅在 BOLT 支持的范围
进行块布局。AArch64 非重定位输入依赖随工具构建的 ADR/ADRP 修复，不能把普通
上游工具默认为等价。loader、libc、辅助运行库被排除；缺少 `.symtab`、Go PC/栈
元数据等情况明确跳过。任意剥离二进制的符号恢复不是当前功能。

基础策略使用 ext-tsp、hfsort+（有重定位）、冷热分离和 BAT；tune 比较取消分裂及
128B 对齐的候选。采集粒度、padding、指令数量与 cache 容量间存在权衡，不能仅
以静态布局好看作为收益。用户可传递 `--bolt-arg=-option=value` 作受控实验。
未来可加入画像驱动 ICP、内联、重定位恢复、BAT 迭代和更多布局搜索；每项都需独立
能力检查、目标 ISA 回归、正确性和原生确认，不能直接默认打开旧实验的激进选项。

## 部署与分发

入口模板是 ELF + 长度编码配置 + footer，不执行 shell，不依赖目标 Python 包。
审计库仅提供路径搜索/加载回调，不注册符号绑定回调。DSO 的 `$ORIGIN` 展开到原
目录，同时保留 RPATH/RUNPATH 类型。ELF 主程序仍是主程序，DSO 仍是 DSO；
静态程序、musl、setuid/secure execution、Go runtime 元数据需要单独适配。

AppImage 与被优化负载的部署包是两种产物。前者可以分发；后者默认绑定原机的 ELF
与路径。AppImage 的 Python/PATH 环境只用于控制工具，不会泄漏到负载解释器。
运行所需原生 helper 已预构建，生成入口仅追加配置，所以不需要在分发机安装 CC。

## 接口扩展顺序

1. 采集后端输出 `tgo.profile.v1`，声明采样/精确计数、进程树、线程与 ROI 范围。
2. 优化器声明 ELF/运行时元数据能力，输出可追溯的逐对象变换和输入身份。
3. validator 记录业务正确性、性能口径、加载证明，允许拒绝或保留原版。
4. 部署器只接受已选对象，并保留关闭优化的路径。

DynamoRIO 目前是计划的后端，没有以空壳宣称支持。Go 的源代码 PGO 或 metadata-aware
重写器也应作为独立策略接入，不与当前 BOLT DSO 变换混为一谈。
