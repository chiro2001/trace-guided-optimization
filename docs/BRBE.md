# Arm BRBE 内网实机测试（0.6.0.dev4）

本版实现 Linux perf 标准分支栈采集、质量检查、映像绑定及 perf2bolt/BOLT 接入。BRBE 硬件采集尚待内网机器验证；已有的约 40% vLLM 结果来自冻结工程，不是本版 BRBE 的收益。

## 先跑这一步

解压 `dist/brbe-testkit-0.6.0.dev4.tar.gz` 后进入其顶层目录，Python ≥3.10 即可运行源码，不需要联网安装 Python 依赖。`probe` 和 PID attach 不需要 C 编译器；启动业务的采集需要 C 编译器构建 ROI/映像发现辅助库。使用与目标内核兼容、包含标准 branch-stack 支持的 perf。

```sh
python3 -m tgo --version
python3 -m tgo brbe probe -o runs/brbe-probe-r1 --perf /path/to/perf
```

第二条命令运行约半秒用户态分支循环，然后用 perf 解码真实分支样本。成功时退出码为 0、`brbe-probe.json` 中 `usable=true`；失败退出码为 1，保存原始诊断，不回退。若样本太少，可换新目录并加 `--duration 2`。不要重复使用已有输出目录。

先回传小诊断包即可：

```sh
python3 -m tgo brbe pack runs/brbe-probe-r1 -o brbe-probe-r1-diagnostic.tar.gz
```

包内有内核/架构、允许 CPU 集合、PMU type/CPU 覆盖与 `caps/branches`、perf 版本、事件属性、质量摘要及 perf 诊断。默认不含业务 argv/env、业务 stdout/stderr、原始 perf.data、ELF 或分支地址摘录。路径、PID、CPU 和采集事件仍会出现在诊断中；打包仅生成本地文件。

## 采集实际业务

最简单是按现有亲和性直接启动业务。训练输入需有代表性，循环负载建议覆盖多个完整业务迭代。

```sh
python3 -m tgo record -o runs/brbe-business-r1 --backend brbe \
  --perf /path/to/perf --event cycles:u --frequency 999 \
  --timeout 180 -- /absolute/path/to/program workload-arguments
```

默认继承业务调用者允许的 CPU 集合。需要限制资源时，显式加例如 `--cpus 0,2,4,6`，并按实机拓扑选择多个物理核心。不要在整条命令外层用单 CPU 的 taskset，使业务主线程和 release 等辅助线程都受同一单核掩码限制。`--cpus` 只包住业务启动命令，perf 控制器保持原有集合；业务程序自己的线程绑核规则仍有效。

附加到已经运行且已经进入循环的业务：

```sh
python3 -m tgo record -o runs/brbe-attach-r1 --backend brbe \
  --perf /path/to/perf --pid 12345 --roi window --delay 3 --duration 15
```

将 12345 换成实际下发线程所在进程的 PID。此命令不暂停、不注入、不修改目标线程亲和性，也不需要 C 编译器。保存采集前后每个现存 TID 的允许 CPU 集合。perf 附加该 PID 的线程并沿用内核继承语义；不会递归覆盖附加前已经独立存在的其它 worker 进程，多进程业务应分别选择真实 worker PID。`samples_per_cpu`、`top_tasks` 说明实际观察到的线程/CPU；只代表采样覆盖，不证明每个线程或核心都被采到。

attach 不自动推断业务环境或回放命令。如需随后原生配对验证，应在期望的业务回放工作目录执行上述采集，再显式关联可重复运行的命令：

```sh
python3 -m tgo bind runs/brbe-attach-r1 -- /absolute/path/to/program replay-arguments
```

本步骤不等同于任意外部进程在线替换。既有在线后端仍仅支持已资格化的 AArch64/4KiB 自有进程及适配器。

已有 `tgo.roi.region()`/C ROI 标记的业务可使用 `--roi marker`；通过标准 perf control FIFO 开关同一个采样事件。Python 方法 hook 仍用 `--python-hook MODULE:CLASS.METHOD`，仅支持启动模式。不要把预热和正式 ROI 混作同一性能分母。

## 数据能否用来优化

实现调用的核心标准选项为：

```text
perf record --no-buildid-cache --buildid-all --sample-cpu --mmap-pages=128
            -e cycles:u -F 999 -j any,u -o perf.data -- WORKLOAD
perf evlist -v -i perf.data
perf script --no-itrace -i perf.data -F pid,tid,cpu,ip,brstack
```

BRBE 是采样时最近的一段分支历史，记录源/目标和内核提供的属性，不是完整指令流，也不要求重建逐条指令。历史缓冲覆盖旧记录属于采样机制，不能当作已统计的 perf 数据丢失。不能把一次成功打开 cycles 事件等同于成功采到 BRBE。

`run.json` 保存实际 collector argv、事件/频率或周期、过滤方式、原始负载 CPU 集合及采集程序身份。`brbe-quality.json` 包含原始 SAMPLE 数与 perf 解码数量、非空栈数、深度分布、完整/部分分支、P/M/未知标记、CPU/TID 分布、LOST/LOST_SAMPLES 和节流记录。未知预测信息保持未知，不伪造为预测命中。

本工具的默认质量门禁为：至少 100 个非空样本、至少一个双端有效分支、空栈不超过 10%、保守损失上界不超过 1%、无内核节流，且原始/解码样本数相符。探测用 20 个非空样本。这些是工具策略，不是 Arm 架构规定，也不是统计收敛证明。正式收益仍需独立配对和留出验证。

LOST 可能包含非 SAMPLE 记录；LOST 与 LOST_SAMPLES 分开报告，其和形成保守上界，不能解释为精确丢失指令比例。质量不通过则保存失败，禁止当作成功 BRBE 画像进入 BOLT。需要调整时使用新目录，优先延长 ROI、降低 `--frequency`，或改用 `--period N`；可用 `--brbe-mmap-pages 512` 增加标准环缓冲，但受当前用户 memlock/内核权限限制。采样频率与周期选一种即可。

`--brbe-min-samples`、`--brbe-max-loss-percent`、`--brbe-max-empty-percent` 可显式调整工具阈值，阈值写入证据。不要为让测试变绿而无说明地放宽门禁。质量分析仅接受本后端生成的普通未压缩 perf 文件；不把 SPE AUX 或压缩 trace 混作 BRBE。

`profile.json` 对本次实际 ELF 核对 perf 记录的 GNU build-id，并保存当前文件摘要和 perf.data 摘要。缺少热映像的匹配 build-id、映像不可读/被替换或多数热点无法解析时拒绝。容器场景应在能读取目标 ELF 的相同路径/命名空间中进行归一。采集与归一期间保留原 ELF，避免升级或替换；JIT/已删除映像不在本轮普通 ELF 重写范围。

## 生成和验证 BOLT 候选

首次实机测试建议显式 `--strategy bolt`，将采集路线和 CPython 可选源码 PGO 分开。准备与目标 ISA 一致的 llvm-bolt/perf2bolt；可通过命令行显式提供路径。

```sh
python3 -m tgo tune --strategy bolt --run runs/brbe-business-r1 \
  -o runs/brbe-tuned-r1 --bolt /path/to/llvm-bolt --perf2bolt /path/to/perf2bolt \
  --image /absolute/path/to/hot-image.so --validate 5
python3 -m tgo evaluate --bundle runs/brbe-tuned-r1 -o runs/brbe-heldout-r1 \
  --pairs 5 -- /absolute/path/to/program heldout-arguments
```

普通 perf2bolt 直接读取标准 BRBE 分支栈，不加 `-nl`，也不加 SPE 专用 `-spe` 或 SPE 桥。使用采集归一时记录的 perf 解码程序，避免手动指定的 perf 在聚合阶段被其它 PATH 版本替代。输入资格仍按 ELF/符号/重定位/运行时结构判断，不查已知应用 SHA 表；标准 BOLT 优先使用保留符号、FILE 和代码重定位的输入。没有稳定收益则保留 baseline。

已有多轮训练可先 `tgo merge`；每份 perf.data 各自映射归一到 ELF 后才合并 fdata，不拼接裸记录、不把独立采样窗口当连续分支历史。多场景用 `--validation-cases` 按最差场景筛选确认，留出只验证。对于已支持的在线 Python 适配器，新 BRBE bundle 的 fdata 可以作为 `live python/stdlib` 的输入；BRBE 实机采集加在线切换的组合仍待实机验证，不能用已有 QEMU 在线结果代替。

## 标准依据与验证边界

参考资料的固定版本及本地摘要位于 `evidence/brbe-reference-20261106/sources.json`：

- [Arm DDI 0601，2021-03，体系结构寄存器手册](https://documentation-service.arm.com/static/606f045785368c4c2b1c198c)：BRBIDR0_EL1 的记录深度和格式；BRBINF 的有效性、源/目标与未知属性。该版本用于字段原理；系统可用性由实际内核接口测试决定。
- [Linux v6.17 arm_brbe.c](https://github.com/torvalds/linux/blob/v6.17/drivers/perf/arm_brbe.c)、[arm_pmuv3.c](https://github.com/torvalds/linux/blob/v6.17/drivers/perf/arm_pmuv3.c)：硬件探测、标准 perf 过滤、记录读取、`caps/branches` 和权限边界。
- [Linux perf-record](https://github.com/torvalds/linux/blob/v6.17/tools/perf/Documentation/perf-record.txt)、[perf-script](https://github.com/torvalds/linux/blob/v6.17/tools/perf/Documentation/perf-script.txt)、[perf UAPI](https://github.com/torvalds/linux/blob/v6.17/include/uapi/linux/perf_event.h)、[perf.data 格式](https://github.com/torvalds/linux/blob/v6.17/tools/perf/Documentation/perf.data-file-format.txt)：分支过滤、采样字段、记录与损失语义。
- [Linux branch-stack 自测](https://github.com/torvalds/linux/blob/v6.17/tools/perf/tests/shell/test_brstack.sh)：以真实分支采样检查支持，而非根据 CPU 商品名判断。
- [LLVM 21.1.8 DataAggregator](https://github.com/llvm/llvm-project/blob/llvmorg-21.1.8/bolt/lib/Profile/DataAggregator.cpp)：标准 `pid,brstack`、MMAP2/build-id 聚合路径。

不写死最低内核版本，发行版可能回移驱动；有 BRBE CPU 也可能受固件、宿主/客体和权限限制。工具不读写 BRBE 特权寄存器，不安装内核补丁，不自动调整 sysctl、不自动 sudo。`caps/branches` 缺失只是诊断提示，实际标准事件和分支样本成功仍可资格化厂商回移实现。

本机105项回归中103项通过、2项因环境跳过。已验证软件接口构造、大小端 record 扫描、真实 perf 解码、损失/空栈/节流/超时拒绝、build-id 绑定、真实 perf2bolt/BOLT 改写及输出等价、attach 亲和性不变和诊断包排除项。标准 ABI 集成夹具是人工构造，明确标记 `hardware_brbe_tested=false`。920B用perf6.14验证了“不支持branch stack”的真实事件拒绝；另以现场编译的ARM ELF完成标准ABI夹具→perf→perf2bolt→BOLT→原生输出等价的软件链路。内网 BRBE 硬件结果、采样覆盖、开销和收益需由实机反馈补齐。
