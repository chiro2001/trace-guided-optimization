# CLI与Agent接口

下面的`tgo`代表`python3 技能目录/scripts/tgo.py`，也可在项目中使用`python3 -m tgo`。

```sh
tgo doctor
tgo capabilities /path/to/image.so
tgo prepare /path/to/program -o work/prepared-new --strip-debug
tgo experiment python -o runs/new-tag --python /path/to/python --cpus 0,2,4,6 --case mixed --seed 101 --heldout-seed 202
tgo experiment python-stdlib -o runs/stdlib-tag --python /path/to/python --train-rounds 1 --rounds 25 --modules 24
tgo evaluate --bundle runs/selected -o runs/heldout --pairs 5 --metric region-name -- /path/to/program heldout-arguments
tgo job start -o jobs/new-tag --timeout 3600 -- python3 -m tgo experiment python -o runs/new-tag --python /path/to/python
tgo job status jobs/new-tag
tgo job stop jobs/new-tag
```

Go工作集先生成：

```sh
tgo workload go prepare -o work/go-train --packages 16 --functions 96 --seed 101
tgo tune --strategy go-compiler --go /path/to/go --cpus 0,2,4,6 --env GOMAXPROCS=4 --env GOFLAGS=-p=4 --metric go-compile -o runs/go-new -- /path/to/python /path/to/tgo/workloads/go_compile.py run work/go-train --go /path/to/go --cache cold
```

`cold`每次用独立构建缓存；`preserve`保留缓存，二者不可混为同一收益。`TGO_VALIDATION_GOCACHE`由验证器提供时优先使用。Go验证会核验实际执行的候选compiler，独立留出复用已记录的Go-aware适配元数据。

受控在线命令：

```sh
tgo live native-check -o runs/native-new --bolt /path/to/identity-capable-bolt --runtime /path/to/libbolt_rt_instr.a --cpus 0,2,4,6
tgo live python -o runs/live-new --bundle runs/selected --image /path/to/libpython.so --python /path/to/python --bolt /path/to/identity-capable-bolt --case mixed --seed 303 --rounds 500 --pairs 5 --cpus 0,2,4,6
tgo live tune-python -o runs/live-search --bundle runs/selected --image /path/to/libpython.so --python /path/to/python --bolt /path/to/capsule-capable-bolt --capsule --seed 101 --heldout-seed 505 --search-pairs 2 --pairs 5 --cpus 0,2,4,6
tgo live tune-stdlib -o runs/live-stdlib --bundle runs/stdlib-selected --image /path/to/libpython.so --python /path/to/python --bolt /path/to/capsule-capable-bolt --capsule --modules 24 --seed 101 --heldout-seed 505 --pairs 5 --cpus 0,2,4,6
```

生成器通过`--help-hidden`检查函数身份与指定VMA能力；普通llvm-bolt未必具备在线能力。给新版本重新采集，`--bundle`与`--image`按本次输入绑定。`live python`不重新选型；复用已观测的留出seed探索新布局后，该seed应改标探索集，再使用新的留出seed。`session.json`分别保留`results`、`validation`、`hardware_execution_proofs`、`transitions`；每次事务有独立fsync journal。对比`original_function_count`、生成函数数与实际入口数，不能用入口拒绝数为零推断全库函数全部迁移。

启动MCP stdio：`tgo agent`。协议消息为逐行JSON-RPC，标准输出仅输出协议消息。当前九个工具：`tgo_doctor`、`tgo_capabilities`、`tgo_brbe_probe_start`、`tgo_brbe_record_start`、`tgo_job_start`、`tgo_job_status`、`tgo_job_stop`、`tgo_manifest_read`、`tgo_live_python_start`。在线工具用结构化字段`job_output/output/bundle/image/bolt/python`启动任务，可选`cpus/case/seed/rounds/pairs/warmups/capsule`；默认`mode=replay`复测固定配置，`mode=tune`可加`heldout_seed/search_pairs/variant/minimum_gain/confirm_candidates`自动选择。`variant`为数组，元素可用ext-tsp/aligned/compact/balanced/lite；固定配置的layout/lite字段与tune模式不混用。长任务立即返回任务目录，再查询状态。

BRBE工具参数和质量门禁见[实机流程](brbe.md)。`tgo_doctor`接受可选perf路径；BRBE工具共用`job_output/output/perf/cpus/event/frequency/duration/mmap_pages`。record增加command或pid、roi/delay/period/timeout；附加模式不接受cpus，默认不改变任何目标线程亲和性。后端不提供任意外部进程在线替换。

任务终态：`complete`、`failed`、`cancelled`、`timed_out`、`resource_limit`。`complete`表示命令完成，是否发布优化版还要读调优清单的`selected`、工作集清单的`published`、验证的`equivalent`与配对wall。`baseline`回退也是有效实验结果。

`capabilities`的`known_input_allowlist=false`说明资格不查已知应用哈希表；`online.static_candidate`及其`requirements`是结构诊断。输入仍须与画像一致，不能跨不同ELF内容套用地址画像。

`job start`接受argv而非shell字符串。外部进程保持调用者指定语义；包含`python -m tgo`的任务会复制工具源码并记录源码清单，同时固化已解析的工具/头文件路径，避免副本没有deps。只保存显式环境与解析出的工具路径，不收集完整环境。`--mem-floor-gib`可用于大编译任务，`--wait-for`等待前序任务进入任意终态以串行化同机测量。

生成原生入口使用ABI3，stat快速路径包含ctime；同长度内容修改后恢复mtime也会触发本次工件摘要复核。老入口原件仍可运行，新入口若引用旧TGO_RUNTIME_DIR模板，会明确要求重建runtime。显式工具路径失效时不静默换用系统另一个版本；C++编译器可用TGO_CXX指定。

MCP的workload默认python-heavy；选择python-stdlib后，用modules替代case/size。mode=replay和mode=tune分别调用固定配置与自动选择。新增在线适配器仍须通过相同PID/源文本对象身份、语义、ROI内硬件命中和回滚。

多负载场景清单示例：

```json
{
  "schema": "tgo.validation-cases.v1",
  "role": "validation",
  "cases": [
    {"name": "常规", "command": ["/path/to/program", "--mode", "normal"], "metric": "business"},
    {"name": "复杂", "command": ["/path/to/program", "--mode", "complex"], "metric": "business"}
  ]
}
```

```sh
tgo tune --strategy bolt --run runs/mixed-profile --validation-cases work/cases.json -o runs/mixed-tuned --image /path/to/program --entry-mode executable
tgo validate runs/mixed-tuned --cases work/heldout-cases.json --pairs 5
```

筛选/确认的清单role必须为validation；冻结候选后，把新输入集合设为heldout，仅验证、不重选。每个场景可提供cwd/env；相对可执行路径和PATH按该场景工作目录解析，命令始终为argv。综合median_reduction_percent保留真实总体统计，排名另用worst_case_median_reduction_percent；equivalent和逐对最小收益必须同时通过。准备大ELF时保留FILE符号；FILE已经丢失时，prepare不能凭空恢复，需使用含符号原件。
