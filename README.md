# TGO：通用原生代码优化工具

0.6.0.dev4增加标准Arm BRBE实机采集后端，内网测试见[BRBE指南](docs/BRBE.md)。dev3及更早wheel保持固定快照，见dist与evidence。应用资格根据实际ELF、ABI、运行时结构与工具能力决定，已移除旧八库应用SHA白名单。SHA保留用于画像/输入绑定、事务一致性及证据复现；未来版本不需要预登记文件哈希。

[BRBE＋920B vLLM数据合并测试包r1](https://github.com/chiro2001/trace-guided-optimization/releases/tag/testkit-0.6.0.dev4-r1)约747 KiB，含工具源码、固定wheel、Agent skill/MCP、实测数据与离线复算脚本，附SHA-256。解压即可运行，需要目标机提供Python≥3.10及配套perf；BRBE硬件采集和收益待内网实测。

920B的[vLLM算子下发实测数据](docs/VLLM-920B.md)已纳入仓库：三轮9进程、42个完整请求，全方案配对wall缩短中位数41.117%；固定当前源码/PGO后的在线增量21.022%，其中BOLT阶段11.634%。主线程CPU192、release等辅助线程194/196/198，逐ROI原始快照可核验。提供原始请求JSON、CSV、BOLT构建记录和独立复算脚本；测量采用设备桩下完整generate＋同步的主机wall。

首个复杂Python混合负载在920B上完成离线BOLT闭环：独立确认三对wall缩短中位数21.10%，另一个未参与选型的seed三对缩短20.80%，语义一致。Go编译对照的首轮PGO和布局候选为-3.03%和-0.33%，因此保留原编译器。范围、原始证据与后续进展见[持续状态](WORK_STATE.md)。这些数据不与此前vLLM全方案41.117%合并。

```sh
python3 -m tgo doctor
python3 -m tgo capabilities /path/to/image.so
python3 -m tgo workload list
python3 -m tgo experiment python -o runs/new-tag --python /path/to/python --cpus 0,2,4,6
```

`experiment python`使用短训练、较长的原生筛选/独立确认和另一seed留出；留出只用于验证与发布门禁，不参与候选重新排名。没有可靠收益时发布原版入口。可用`record`、`tune --run`与`evaluate --bundle`分步组织任意负载。QEMU只用于画像，性能使用原生wall。

```sh
python3 -m tgo job start -o jobs/new-tag --timeout 3600 -- python3 -m tgo experiment python -o runs/new-tag --python /path/to/python
python3 -m tgo job status jobs/new-tag
python3 -m tgo job stop jobs/new-tag
```

TGO任务自动复制工具源码；已启动任务不会读取后续修改的工具版本。取消时核验监督器身份并清理自有后代，包含独立会话中的子进程；大编译任务可使用`--mem-floor-gib`。任务、输出和失败目录不覆盖。

Go通过自身PGO与私有链接器更新PC、栈和GC元数据，使用`--strategy go-compiler`，不直接按普通ELF改写Go运行时。工作集可用`workload go prepare`生成，冷缓存与保留缓存分开测量。Go包装器主线程PMU不代表编译器进程树PMU。

现有二进制的BOLT优化不要求CPython源码；可选源码PGO通过`--python-source`接收匹配版本的归档，并检查源码版本及重建ABI。生成ELF经过程序头归一和真实加载/语义验证；对齐修复不修改节内代码、数据或虚拟地址，不能按目标页大小映射的工件仍拒绝。

Agent入口为`python3 -m tgo agent`（MCP stdio），提供诊断、BRBE探测/采集、任务管理、在线Python任务和清单读取九个工具。[技能](skills/tgo-optimize/SKILL.md)支持源码工作区与安装副本，可通过`TGO_PROJECT_ROOT`定位项目；[接口说明](skills/tgo-optimize/references/interface.md)包含具体调用。

`live tune-python`已完成五种在线配置筛选、独立确认和新seed303留出。选中ext-tsp，五对确认中位数7.66%、留出8.48%，留出最小6.15%，语义/新代码硬件命中/回滚通过；迁移包含解释器主循环的5718个入口和256项私有分派表。MCP的`tgo_live_python_start`支持`mode=tune`或固定配置`mode=replay`。各会话完成后均回滚退出，不自动部署到外部进程。

通用在线后端已在920B的AArch64/4KiB页上完成自有进程原生门禁和长驻Python闭环。`live native-check`现场编译/插桩未知ELF；`live python`复用现有画像，在同一PID与原对象图上构建、切换、交替AB/BA并回滚。新入口须在业务ROI内有硬件执行断点证明；探针与预热不计入性能样本。Python混合负载三对中位数缩短8.04%，最小7.29%，切换5717个入口；源库5751个有大小函数中34个未生成唯一对应入口，保留原代码。它与离线20.80%是不同执行路径和分母，详见[实测报告](REPORT.md)。

`online.static_candidate`只表示静态条件。实际提交还需原状态/函数地址身份、旧帧、CFI、线程同步与回滚门禁；现阶段提供自有进程与内置Python工作集，不支持任意未知进程attach，也未资格化BTI/PAC、x86在线或Go二进制在线。此前七库实验继续保存在独立冻结工程。

```sh
python3 -m tgo live native-check -o runs/native-new --bolt /path/to/identity-capable-bolt --runtime /path/to/libbolt_rt_instr.a --cpus 0,2,4,6
python3 -m tgo live python -o runs/live-new --bundle runs/selected --image /path/to/libpython.so --python /path/to/python --bolt /path/to/identity-capable-bolt --case mixed --seed 303 --rounds 500 --pairs 5 --cpus 0,2,4,6
python3 -m tgo experiment python-stdlib -o runs/stdlib-new --python /path/to/python --image /path/to/libpython.so --train-rounds 1 --rounds 25
```

`python-stdlib`编译真实标准库源码，按不同seed选择模块子集，检查AST统计与字节码摘要。gem5提供`workload gem5 prepare`生成无libc的AArch64 guest和独立SE配置；宿主`m5.simulate`的原生wall用于收益，guest校验和与simTicks/simInsts/simOps用于语义。gem5最终混合O3/Minor画像后，按最差场景确认，seed505留出分别缩短9.02%/11.79%，各五对且语义/实际映像通过。只用O3训练的旧方案曾使Minor退化，失败数据同样保留。

构建依赖可运行`scripts/bootstrap.py`或通过`TGO_LLVM_BOLT`、`TGO_LLVM_BOLT_RECOVER`、`TGO_PERF`、`TGO_QEMU_HEADER`、`TGO_INSTALL_PREFIX`等变量提供。依赖下载锁与应用优化资格分离。历史工具目录保持只读，本目录保存新代码、工件和数据。


输入准备可用`prepare IMAGE -o NEWDIR --strip-debug`；保留FILE/函数符号并逐范围核验代码、数据、BSS和执行布局，再对准备后的实际文件采集。多负载使用`tune --strategy bolt --validation-cases FILE`，按最差场景中位数筛选并逐对确认；冻结后`validate --cases`运行role=heldout的新输入集。发布入口默认继承调用者亲和性，`--publish-affinity workload`才嵌入测试CPU集合。
