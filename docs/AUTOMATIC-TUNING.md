# 自动调优与回退

输入仍是负载的 argv、工作目录、显式环境和 ROI。`tune` 执行采集、对象选择、
对象重写、组合验证和独立确认，最后生成原生 ELF 入口。当前没有常驻后台服务，
不会把新代码热补丁写进正在执行的进程；attach 的画像用于后续启动的入口。

## 多轮训练和画像合并

新鲜训练默认 `--train-runs 3`。第一轮实际探测后端，后续锁定同一后端和事件。
原始记录分别放在 `training/capture-000` 等目录，合并结果位于 `capture/`。
`--train-runs 1` 保持单轮流程；通用 BOLT 策略的 `--run` 不增加采集次数，CPython
专用策略仍继续编译和各阶段训练。Go 策略分别记录三轮实际
compiler CPU profile，使用独立冷 cache，再通过 Go pprof 合并。自动发现 Go compiler
后立即转入专用策略，不继续执行多余的硬件采集。

`tgo merge -o SET CAPTURE...` 创建引用不可变源证据的画像集合。QEMU 按每轮独立
load bias 转为 ELF 地址；perf/SPE 为每份原始数据独立调用 perf2bolt，再通过
merge-fdata 累加计数，不拼接不同 ASLR 空间中的地址。热点选择中，QEMU 指令计数
相加，perf/SPE 采用每轮采样份额的均值，未出现的库按零计算。BOLT 的画像权重为
计数之和，因此更长或命中更多样本的 ROI 贡献更多。

默认要求相同的运行命令、工作目录、显式环境、ROI 模式/时间边界及 Python hook。
hook 的辅助脚本目录可随训练轮次改变。不同参数的有意训练变体需显式
`--allow-workload-variation`，差异字段和所有源目录记入 profile.json；该选项仍不允许
混合采样事件、周期、频率、branch-stack 模式或同路径的不同 ELF 内容。
集合再合并时展开至叶子，拒绝重复输入，防止隐式重复计数。

源 run.json、profile.json、perf.data 或 QEMU trace/maps/预聚合文件均绑定 SHA256，
重写前后校验；漂移会终止调优，不能作为单库失败忽略。需保留被引用的源目录。
Go CPU profile 使用其专用策略合并，不接受通用 merge。合并的命令入口采用第一份
记录的 replay 命令与环境，允许变体时应明确选择第一份来源。

多轮训练与独立性能确认是不同阶段。增加训练覆盖不意味着放宽逐对收益门槛，也
不能把训练时长、画像计数或初筛当成已确认的性能收益。

perf 采集与权限探测统一使用 `--no-buildid-cache`：仍将 build-id 保留在 perf.data，
但不向全局 cache 写入源文件硬链接。920B-47 的固定容器镜像中，默认 cache 写入
会使后续采集的大量内核 MMAP 文件名变成 `/ (deleted)`；关闭该写入后连续三轮
复现均未出现此问题。归一化还记录已解析/未解析的采样份额与 DSO 名称；多数样本
不能对应可读 ELF 时保存 profile.json 并拒绝继续，避免将缺失映射当作热点变化。
大量匿名/JIT 代码也会触发该覆盖检查，当前 BOLT 管线不能重写这些代码。

## 后端与对象选择

`--backend auto` 使用短暂的 `/bin/true` 探测：若 SPE retired-branch 事件和实际采集
所需的 AUX 缓冲能打开，优先使用 SPE；否则使用可打开的 cycles recorder；再否则
使用 QEMU。探测使用与实际采集相同的 SPE 缓冲配置，避免小缓冲成功却在负载启动前
因 mlock 权限失败。短探测不证明
采样覆盖，真实 ROI 的原始数据和归一化仍会检查。显式 `--event` 按给定事件选择
perf/SPE，不悄悄更换事件，也不因真实负载失败而重跑负载。

按 ROI 计数/采样权重排序，默认最多尝试三个可优化对象。排除 loader、libc、工具
辅助库以及缺少专用 adapter 的 Go/子进程主 ELF。`--image` 可限制对象，`--top`
调整数量。对象路径、SHA256、具体跳过或失败原因保存在 build manifest。

默认 `--bolt-mode auto` 针对每库决定：

- 保留重定位的对象使用标准 BOLT。
- 无保留重定位的 AArch64 DSO，在匹配 recovery 工具可用且能力检查通过时使用
  recovery，并保留旧入口 trampoline、只处理有画像的函数。
- 其他对象使用标准策略。stripped ELF 允许 BOLT 利用其动态符号/unwind 信息
  识别函数；不能识别的范围不会由工具假定为安全。

通用策略不启用 assume-code-pages-safe。`standard` / `recover` 可固定策略；标准手工
模式仍需显式 `--allow-stripped` 才处理缺 `.symtab` 的输入。

## CPython 与完整八库配方

`--recipe auto` 默认识别已核实的 eager-v12 八库原始 SHA256。只有全部输入匹配才
使用这份配方；版本变化退回通用策略。`--recipe standard` 禁用配方，显式
`--recipe eager-v12` 则在身份不匹配时报错。NumPy 这类带非零 ELF entry 的共享库
仍按 DSO 处理，不能仅凭 e_entry 把它当作子进程主程序。

识别配方且有 GCC 时，`--strategy auto` 进入 CPython 策略；其他 CPython 负载可
显式 `--strategy cpython`。策略下载并核对精确版本的源码，生成 GCC 插桩解释器，
仅在 marker ROI reset/dump 画像，再构建保留重定位的 PGO/LTO libpython。
需要匹配版本的 GCC、g++、make，以及共享 libpython 和显式 marker ROI。
不安装替换原解释器，运行时由新 ELF 入口重定向私有库。

AArch64 有 BOLT instrumentation runtime 时，`--python-profile auto` 为新 Python
生成原生 BOLT 插桩库，逐 ROI reset/dump 并合并 fdata。hook 地址从当前 archive
对象与实际链接产物推导，检查非重定位指令及运行时 maps。原生边计数只用于对应
SHA256 的 Python，不与 SPE 采样单位混合。`--python-profile sample` 可显式保留
采样路线；`native` 在条件不满足时直接报错。

已知八库配方默认使用 QEMU 为其余七库记录完整 ROI。`--max-roi-windows 1`
仅限制记录窗口，后续请求仍正常执行；不会修改用户负载参数以缩短正确性验证。
混合画像按库独立归一化，热点选择沿用基线 discovery 权重。七库采用 BIND_NOW、
全函数 recovery、4B 布局、清理和 PLT 策略；其中代码页恢复假设严格绑定已知原始
SHA256，不自动推广至任意二进制。

`--python-build DIR` 可复用本轮完成的构建，但会检查负载 contract、原始/输出 ELF
及全部 GCC 训练画像 SHA256。`--library-capture DIR` 可复用完整 QEMU 采集，检查
原始证据及逐库 ELF 身份。这些选项减少开发迭代时间，最终报告应分别列出缓存复用
与从头构建的结果。`--library-backend spe|qemu` 可显式选择库画像路线。

源码包和 instrumentation archive 已纳入下一版 AppImage 打包流程；现有 0.4.0
分发文件不包含该策略。构建 Python 仍需要目标环境提供 GCC/g++/make。


## 失败和组合搜索

单个对象的聚合、重写、格式检查或依赖搜索修复失败时，记录该对象的日志并继续
其他对象。生成入口只重定向成功的对象，失败对象沿用原版。若一个也未成功，保留
失败记录，不生成伪装为优化成功的候选。源 ELF、原始画像或优化后对象的身份变化
会直接终止，不作为可以忽略的重写失败。

默认 `--image-search adaptive` 先验证所有成功对象的组合。如果组合正确且初筛
中位数达标，进入布局比较；否则探测逐个移除对象的组合及单库组合。每个探测只跑
一对，最多两个有希望的子集再跑完整初筛。所有子集复用相同的 DSO，不重复重写。

`--image-search joint` 仅比较整组。`--image-search all` 对最多四个对象探测所有非空
真子集，仍只把最有希望的两个子集送入完整初筛。大对象数的默认回退是线性搜索，
不会自动展开指数数量的组合。`--layout ext-tsp` 等可限制布局，默认比较 ext-tsp、
compact、aligned。

## 初筛、确认与证据

初筛正确且完整、中位数超过门槛的候选进入排序。初筛不是收益证明，某一对轻微
回退不会直接淘汰整个候选。独立确认使用新进程 AB/BA 交替执行，要求每一对均
正确且超过 `--minimum-gain`，默认 0.5%。默认最多确认两个候选，第一个未通过可
继续下一个；`--confirm-candidates` 可调整上限。全部失败则最终入口调用原版。

Go compiler 策略共用相同的初筛/确认规则，另检查实际 compiler 执行和独立 cache。
最终发布前重新检查已选对象的 SHA256，入口继续提供运行时版本漂移检查。

每次验证写入独立的 `validations/<id>/`，包含逐 arm 的 stdout、stderr、真实映射、
确定性结果、ROI/PMU 和该轮 validation JSON。`evidence_directory` 与
`evidence_prefix` 标明原始位置；顶层 `validation.json` 是最新一轮的快捷入口。
初筛、子集探测和独立确认的原始文件都保留。

采集、构建和验证的超时会清理工具创建的进程组，包括忽略 SIGTERM 的后代进程。
attach 的原目标在工具进程组之外，不因停止采集而终止。
