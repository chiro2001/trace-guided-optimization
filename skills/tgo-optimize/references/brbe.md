# BRBE 实机工作流

使用技能入口 `scripts/tgo.py`，下文 `tgo` 代表该入口或已安装 CLI。需要 Linux AArch64 和支持标准分支栈的 perf；不按 CPU 商品名或发行版版本猜测支持，不自动变更系统权限。

```sh
tgo brbe probe -o runs/brbe-probe-new --perf /path/to/perf
tgo brbe pack runs/brbe-probe-new -o brbe-probe-diagnostic.tar.gz
tgo record -o runs/brbe-business-new --backend brbe --perf /path/to/perf -- /path/to/program arguments
tgo record -o runs/brbe-pid-new --backend brbe --perf /path/to/perf --pid 12345 --roi window --delay 3 --duration 15
```

先读 `brbe-probe.json` 的 `usable`，失败时读原始诊断。显式 BRBE 不回退，成功 record 还需 `run.state=recorded`、可核对 GNU build-id 的热点和 `brbe-quality.usable=true`。正式默认至少100个非空栈、空栈≤10%、保守损失上界≤1%、无节流且原始/解码样本数相符；阈值是工具策略，不是代表性或统计收敛证明。

BRBE 不是完整指令流。报告样本量、深度、部分分支、预测未知、每 CPU/TID 的观察覆盖、LOST 与 LOST_SAMPLES。历史覆盖旧记录不是 perf 丢失；保守损失上界也不是精确丢失指令比例。不要在完整画像中插入伪造分支、拼接窗口或把未知标记改为预测命中。多次采集各自映射后才合并 fdata。

默认继承调用者 CPU 集合；`--cpus` 只约束新启动的业务。不要把多线程业务外层整体 taskset 到单核。attach 不编译或注入辅助库、不修改目标亲和性，保存 before/after 各TID集合；既存独立 worker PID 需要另行指定，不能宣称自动覆盖整个进程树。

先用 `--frequency` 降低采样开销，或改用 `--period`；缓冲可用 `--brbe-mmap-pages` 调整。输出目录不可覆盖。`brbe pack` 白名单包不带业务argv/env、完整perf.data、ELF或分支地址摘录；它只生成本地文件。

```sh
tgo tune --strategy bolt --run runs/brbe-business-new -o runs/brbe-tuned-new --bolt /path/to/llvm-bolt --perf2bolt /path/to/perf2bolt --validate 5
```

不加 `-nl` 或 `-spe`，不使用 SPE 桥。显式 perf 路径会贯穿归一和 BOLT 聚合。attach 后优化前需 `tgo bind RUN -- REPLAY_COMMAND`，保持预期工作目录和显式业务环境。独立确认和留出沿用现有门禁；不保证 BRBE 一定优于已有画像，不将旧40%结果移作新后端收益。

MCP 新工具 `tgo_brbe_probe_start` / `tgo_brbe_record_start` 都立即返回持久任务。传 `job_output/output/perf`；record 的 `command` 与 `pid` 二选一，attach/window 必须给 duration，attach 不接受 cpus/marker。用 `tgo_job_status`、`tgo_manifest_read` 查看完成状态与质量。

源码项目的 `docs/BRBE.md` 含完整内网测试步骤和固定版本官方参考；`evidence/brbe-reference-20261106/sources.json` 为资料摘要。ABI夹具和920B拒绝路径不等于BRBE硬件实测，交付时保留 `hardware_brbe_tested=false`，由实机反馈更新。
