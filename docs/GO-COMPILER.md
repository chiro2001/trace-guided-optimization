# Go 编译器的画像和函数布局

Go strategy 面向 `go build`、`go test` 等调用 SDK compiler 的负载。输入是原命令，
输出是私有 compiler、原生 tool adapter 和原生 ELF 入口。源 SDK、原编译器及用户
构建命令保持可用。一般 Go 应用程序的代码重排尚未接入此策略。

## 链路

1. 解析 `go env`、compiler 身份及原构建设置，确认 SDK 包含源码。
2. 用 `-toolexec` 给严格匹配完整路径的 compiler 添加 `-cpuprofile`。每 PID 独立
   文件；其他工具直接执行。训练使用独立 build cache。
3. 合并 pprof 时使用 `-symbolize=none`，保留 PGO 所需的 `Function.start_line`，再用
   `go tool preprofile` 校验。记录原始 profile 的 SHA256，复用时再次验证。
4. 按原编译器的 CGO、架构、GOEXPERIMENT、trimpath、gcflags 等设置重建候选。
5. 通过候选编译并执行覆盖泛型、接口、异常、GC、闭包与并发的 fixture。
6. AB/BA 验证实际负载。检查退出、确定性输出和确实执行了候选 compiler；筛选后
   另外做独立确认。每对都达到收益门槛才发布，失败或无收益保留原工具。

## 候选

| 名称 | 变化 |
| --- | --- |
| `pgo` | 用工作负载 CPU profile 代替 SDK 画像 |
| `compact` | 同上，热内联预算 256 |
| `aligned` | 同上，函数对齐 ARM64 128 字节 / AMD64 64 字节 |
| `hybrid` | 保留 pprof 元数据，目标画像 sample 权重约 25%，SDK 原画像约 75% |
| `layout` | 保留 SDK 原 PGO，另按目标画像调整包内函数次序 |
| `layout-runtime` | 同上，也调整 runtime 的 Go 函数，保持其汇编位置 |

官方 compiler 已有默认 PGO；替换成局部负载画像可能回退。训练样本比例不等于 CPU
时间比例或运行性能；混合只产生需要重新验证的候选。

`layout` 从 preprofile 的 named edges 聚合权重，贪心连接高权重函数链，限制每链
最多 64 个函数。自建 linker 在构造 compilation-unit/global Textp 之前稳定重排
各包普通 STEXT；包依赖顺序、DupTextSyms、外来内联符号的位置、runtime 和 FIPS
范围保持原布局。`layout-runtime` 允许调整 runtime/internal-runtime 的 Go 函数，
汇编函数保持原位置。overlay 不修改 GOROOT。重定位、PC 表、栈映射、GC metadata 和
ARM 分支 trampoline 均仍由 Go linker 正常生成。

保存 `function-order.txt`、`layout.tsv`、`layout.json` 和 `go tool nm` 的最终地址。
同名局部/ABI 符号不能仅凭名字唯一识别，因此单列为 ambiguous，不猜测 PC。
唯一匹配函数的包内最终地址必须与请求顺序一致；没有移动或出现逆序即拒绝候选。
当前 source anchor 不兼容的 SDK 会明确拒绝布局变体，其他候选可以继续。

## 入口与 cache

入口动态追加 GOFLAGS 的 `-toolexec`，保留调用者其他 GOFLAGS；已有 toolexec 时
报冲突。helper 返回包含候选 SHA256 的 tool identity，使 Go cache 不会错误复用
原 compiler 的构建结果。自建 linker 的身份还包含函数顺序文件与 runtime 模式，
避免复用其他画像的布局。`TGO_DISABLE=1` 执行原工具和原版本信息。
入口和 helper 的 stat 不同时会校验 SHA256；同内容文件可跨 namespace 使用。
入口验证后更新传给 helper 的 stamp，正常编译调用继续使用快速检查。

默认验证每 arm 使用独立冷 build cache，并先做两对不计入收益的预热。文件系统
缓存、NUMA、CPU 频率和同机其他任务仍可能影响 wall time，应在稳定资源条件下
确认。当前计时指标是完整编译命令的 wall time，不能将其直接称为前端 stall 改善。
负载自己的产物正确性测试可以在 driver 中执行；stdout 相同不等于完整语义证明。

新训练默认执行三轮，可用 `--train-runs N` 调整；各轮独立冷 GOCACHE，原始 profile
保存在 `capture/profiles/round-000/` 等目录，stdout/stderr 和时长分别记录在
`training_trials`。Go pprof 合并全部轮次，保留 PGO 所需元数据。`--run` 复用时不增加
训练次数，并给复制的原始 profile 加序号，避免不同目录中的同名 PID 文件互相覆盖。

AppImage 携带 adapter，不要求目标机安装 C 编译器；Go 工作负载本身仍需对应的完整
SDK。产物绑定记录的原路径，移动 SDK 或升级 compiler 后需要重新调优。
