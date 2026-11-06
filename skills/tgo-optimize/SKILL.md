---
name: tgo-optimize
description: 使用TGO对Linux原生负载做结构化资格诊断、Arm BRBE标准perf采集、BOLT或Go编译器调优、独立确认和留出验证，并管理持久调优任务。适用于CPython、gem5、Go编译和已有TGO工件诊断。
---

使用本技能的`scripts/tgo.py`调用CLI。源码项目可通过`TGO_PROJECT_ROOT`指定；源码工作区中的技能可自动定位相邻项目。安装副本可读取`references/local-install.json`。不要根据已知应用SHA或固定库清单决定是否优化。

先运行`doctor`和`capabilities ELF路径`，按实际结构和工具能力选择路线。哈希用于把本次画像、工件与进程事务绑定到同一输入；新版本应重新采集画像，不要求预登记哈希。缺少可迁移函数、展开信息或运行时元数据时，保留可执行的范围和明确拒绝原因。

CPython可以识别解释器入口、分派表等特殊点位。普通BOLT优化现有二进制不要求重建解释器；可选PGO源码输入用`--python-source`，源码声明版本与重建ABI必须和运行解释器一致；版本号相同不代替ABI和语义验证。Go包含PC/栈/GC元数据，走`--strategy go-compiler`，不要直接当普通ELF在线改写。

长任务用`job start`，轮询`job status`，需要取消时用`job stop`。`python -m tgo`任务会冻结自身源码；不要修改任务目录下的source或已有训练记录。使用新的输出目录，保留失败、退化和拒绝记录。

BRBE实机采集先读[BRBE流程](references/brbe.md)，运行`brbe probe`验证真实用户态分支栈，再显式`record --backend brbe`。CPU名、内核版本、caps或一次cycles事件打开均不足以证明可用。BRBE是采样分支历史，不是完整指令流；保持标准perf.data→perf2bolt，不加-nl/-spe。默认继承亲和性，attach不修改目标线程集合；保存质量、覆盖和映像绑定，不能把无BRBE机器上的ABI夹具测试写成硬件验证。

Python常规闭环使用`experiment python`；真实源码编译用`experiment python-stdlib`：短训练、较长的原生筛选和确认、另一seed或模块子集留出。也可用`record`→`tune --run`→`evaluate --bundle`组织其它负载。候选仅按训练/筛选与独立确认选取，留出结果可拒绝发布，不能据此重新排列候选。没有稳定收益时，允许发布原版入口。

大ELF的资格诊断只读所需元数据，不必完整读取DWARF。若要准备调试精简输入，用`prepare IMAGE -o NEWDIR --strip-debug`，它保留FILE与函数符号，并核验全部装载内容和BSS形状；对准备后的实际映像重新采集。不要用会丢失FILE符号的裁剪方式处理含编译器冷片段的输入。

多种负载共用同一优化映像时，用`--strategy bolt --validation-cases FILE`。逐场景筛选/确认，排名采用`worst_case_median_reduction_percent`，不能只看综合中位数掩盖退化。选型完成后再用`validate --cases`读取role=heldout的新输入集合。不同场景表现冲突时，尝试代表性混合画像或保留原版。

报告实际加载/执行与语义等价、原生wall、独立配对数量、样本离散、准备成本和失败。QEMU仅用于画像；Go包装器主线程的PMU不等于编译器进程树PMU。源码/PGO、BOLT和在线快捷绑定的收益分开。库数、生成函数数和实际切换入口数分别报告。

在线路线当前支持Linux AArch64/4KiB页上的自有进程：`live native-check`验证现场新编译ELF的函数身份、TLS、异常和多线程回滚；`live python`用已有画像启动自有长驻Python，保留PID、原对象图和缓存对象，交替AB/BA。业务ROI内须有新代码硬件命中，探针和预热不计入收益。主线程与辅助线程使用不同物理CPU；不要把多线程业务整体绑单核。`online.static_candidate`仅作静态诊断，实际加载仍需原状态/函数身份、旧帧、CFI、同步和回滚门禁。现阶段不支持任意外部进程attach、BTI/PAC、x86在线或Go普通ELF在线。

需要搜索在线配置时用`live tune-python`，比较布局、精简模式和优化级别，再用全新会话独立确认和新seed留出；每个会话都先回滚再退出，只推荐已验证配置。已有配置复测用`live python`。`--capsule`需要生成器显式提供runtime-online-capsule能力，用于保留原代码并由运行时管理入口；新生成器先过native-check。在线代码包不能直接当独立可执行文件发布。

真实标准库在线负载用`live stdlib`或`live tune-stdlib`，持有同一批源文本对象执行AST/字节码编译。MCP用workload=python-stdlib和modules选择，默认workload=python-heavy；不同工作集的专用参数不能混用。

具体命令、MCP工具和状态字段见[接口](references/interface.md)。
