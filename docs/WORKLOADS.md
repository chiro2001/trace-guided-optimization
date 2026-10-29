# 负载协议

## Python microbench

`benchmarks/python_frontend.py` 包含方法调用、属性、字典查找和整数控制流。预热在
ROI 外，确定性 checksum 在 ROI 外输出。优化对象是实际加载的 libpython 或主 ELF，
需有匹配的符号；发行版剥离过的库会被跳过。可用 bootstrap 的 `python` 组件构建。

```sh
python3 -m tgo tune -o runs/python --backend perf --roi marker --metric decode \
  --image libpython3.12.so.1.0 -- deps/install/bin/python3.12 benchmarks/python_frontend.py
```

采集可以使用较短训练循环，再为验证绑定较长、同分布的命令。训练命令与验证命令
差异必须在报告中说明，验证两臂参数一致。

## 原生前端合成测试

`scripts/build_bench.py` 构建有重定位的 `libfrontend.so`，32 个热函数分别按 4KiB
对齐，以制造明显的代码布局/取指压力；不是现实程序收益的替代。Python driver
通过绝对路径 ctypes/dlopen 调用它，用于检验 DSO 重定向、BOLT、PMU 和 ROI。

```sh
python3 scripts/build_bench.py
python3 -m tgo tune -o runs/native --roi marker --metric decode \
  --image libfrontend.so -- python3 benchmarks/native_frontend.py
```

## Go 编译

`sh benchmarks/go_compile.sh` 用独立空 GOCACHE，串行编译 `cmd/compile`。
应由 perf 启动该脚本，以覆盖 `go` 和实际 compiler/assembler/linker 子进程。
同一个用户、同一工具链、相同缓存策略是比较前提。

`tune --strategy go-compiler` 会自动为 compiler 子进程记录 CPU profile，私有重建
compiler，并搜索 PGO/内联/对齐/混合画像及包内函数布局。布局由 Go linker 在生成
PC/栈/GC metadata 之前完成。验证证明实际执行候选 compiler，默认冷缓存与两对
不计入结果的预热。可复用完整捕获的 `capture`，不必重跑训练。

```sh
python3 -m tgo tune --strategy go-compiler --go deps/install/bin/go \
  -o runs/go-compile --timeout 900 -- sh benchmarks/go_compile.sh
```

完整 SDK 含源码是前提；一般 Go 应用的二进制后处理仍被 BOLT 策略跳过。当前 ARM
编译测试中已实现链路和正确性检查，但尚未确认稳定收益，不能把“生成了 compiler”
当成性能结论。详见 [Go compiler](GO-COMPILER.md)。

## vLLM decode

`benchmarks/vllm_decode.py` 固定纯 eager、关闭 cudagraph，模型加载、预热请求和
prefill 在 ROI 外。每次测量包含四次 `engine.step()` 及各自的
`torch.npu.synchronize()`，逐请求产出 decode 指标和确定性 token JSON。
它面向已有 vLLM/Ascend 环境；普通 Python 环境不自动下载模型或初始化设备。

已有 CPU-stub 镜像需要通过其 `/opt/va26-scripts/run_real_test_official.sh` 初始化
stub，再调用本 adapter。应在容器内部执行 TGO，挂载工具、输出目录及同一负载路径；
在宿主仅采样 `docker run` 客户端不能得到容器内 decode 画像。AppImage 在无 FUSE
容器中可先提取并执行 AppDir/AppRun，或使用 extract-and-run 模式。

CPU-stub 的收益仅解释为主机侧 decode 路径，不能外推为真实 NPU 吞吐、数值精度
或端到端延迟。正式测量还需固定 CPU、NUMA、镜像、模型配置、软件/DSO 身份与输出，
且应与其他实验串行，避免共享 CPU 干扰。原有 a3 上任务和环境无需修改。

仓库提供固定镜像的复现入口，在 920B-47 的工作区执行：

```sh
python3 scripts/reproduce_vllm.py --sudo --cpu 240 \
  --cpus 240,242,244,246,248,250,252,254 -o runs/vllm-fresh
```

默认使用源码与私有 SDK，不传缓存参数即从发现热点开始，自动进行 CPython ROI
PGO/LTO、Python 原生 BOLT 和七库 QEMU 训练，再构建八库并独立确认。源码源包可从
已核对的下载缓存取得；网络关闭的容器中需预先运行 bootstrap/打包准备依赖。
`--tool /absolute/path/tgo.AppImage` 用相同条件验证分发文件，模型路径可由
`--models` 指定。CPU 和内存节点应按主机资源调整，避免与其他性能测试共用。

开发中可添加 `--run DISCOVERY --python-build BUILD --library-capture QEMU_CAPTURE`
复用已核验的本轮证据。脚本会打印实际 Docker 命令；复用模式与从头模式应分别报告。
实际结果以 `tune.json` 的独立确认和 `build.json` 的八库身份为准。
