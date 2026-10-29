# 920B-47 快速运行

下面使用本轮 0.3.0 AppImage，不需要安装额外 Python 包或 C 编译器。

```sh
cd ~/projects/trace-guided-optimization
TGO_APP="$PWD/dist/0.3.0-final/tgo-0.3.0-aarch64.AppImage"
"$TGO_APP" --appimage-extract-and-run doctor
```

## 新入口放在原 Python 旁边

```sh
python3 -m venv --without-pip build/demo-venv
taskset -c 210 "$TGO_APP" --appimage-extract-and-run tune \
  -o runs/demo-native --backend qemu --roi marker --metric decode \
  --image libfrontend.so --entry "$PWD/build/demo-venv/bin/python3.tgo" \
  --entry-mode executable -- "$PWD/build/demo-venv/bin/python3" \
  "$PWD/benchmarks/native_frontend.py" --library "$PWD/build/bench/libfrontend.so"

build/demo-venv/bin/python3.tgo benchmarks/native_frontend.py
TGO_DISABLE=1 build/demo-venv/bin/python3.tgo benchmarks/native_frontend.py
```

本例的 `libfrontend.so` 已在 `build/bench/`。需要重新构建时使用
`python3 scripts/build_bench.py`，仅构建测试负载时需要编译器。
CPU 集合按实际空闲核心调整；所有重复运行继承相同 affinity。

Python 实负载可使用 `deps/install/bin/python3.12 benchmarks/python_frontend.py`，
`--image libpython3.12.so.1.0`。自动策略会尝试 stripped ELF 可识别的函数；不能
处理的对象保留原版，并记录原因。

## Go 编译

本机已下载完整 Go SDK。默认使用空 build cache、两对预热、三对筛选和五对独立
确认；未确认获益时自动保留原版。

```sh
taskset -c 240,242,244,246 "$TGO_APP" --appimage-extract-and-run tune \
  --strategy go-compiler --go "$PWD/deps/install/bin/go" \
  -o runs/my-go-compile --timeout 900 -- sh benchmarks/go_compile.sh
```

只尝试包内布局可加 `--go-variant layout`，也可复用
`--run runs/go-compiler-arm-r1/capture`。完整命令入口默认是 `runs/my-go-compile/run`。
候选与真实执行路径保存在 tune/build/validation JSON 和 tools.tsv 中。

分发功能 smoke 从 PATH 移除 C 编译器和 Go，用显式 SDK 执行 `go build`，然后从
项目外目录执行候选入口。它不用于证明收益：

```sh
python3 scripts/smoke_go_appimage.py "$TGO_APP" \
  --go "$PWD/deps/install/bin/go" --output runs/my-go-distribution-smoke
```

## 现有 vLLM CPU-stub 镜像

在容器内部采集。镜像已有设备层桩，不挂载 NPU 设备；模型目录只读。
以下 CPU 集合是本轮实测配置，使用前按本机任务安排调整。

```sh
TGO_ROOT="$PWD"
TGO_BUNDLE="$PWD/runs/my-vllm-tuned"

sudo -n docker run --rm --pid host --network none \
  --cpuset-cpus 216,218,220,222 --cpuset-mems 0-3 \
  --memory 24g --memory-swap 24g --pids-limit 2048 \
  --cap-add SYS_ADMIN --cap-add IPC_LOCK --security-opt seccomp=unconfined \
  -v "$TGO_ROOT:$TGO_ROOT" -v "$HOME/models:/models:ro" \
  -e PY="$TGO_ROOT/benchmarks/container_entry.sh" \
  -e TGO_APP="$TGO_APP" -e APPIMAGE_EXTRACT_AND_RUN=1 \
  -e TGO_BUNDLE="$TGO_BUNDLE" -e TGO_CONTAINER_ACTION=tune \
  -e VLLM_ENABLE_V1_MULTIPROCESSING=0 -e OMP_NUM_THREADS=1 \
  -e TASK_QUEUE_ENABLE=1 -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e XDG_CACHE_HOME="$TGO_ROOT/build/vllm-demo-cache" \
  --entrypoint /bin/bash \
  sha256:a6314dddb8665748c456a4ba3aee5ffd985e366689327889b9b072d4ce18bf97 \
  /opt/va26-scripts/run_real_test_official.sh \
  "$TGO_ROOT/benchmarks/vllm_decode.py" --requests 16 --warmup-requests 1 --roi-cpu 216
```

该命令在容器内完成采集、选库、布局比较、独立确认和入口生成。自动探测 SPE / perf，
默认选择三个热点对象并逐库决定 recovery/standard；无需提供库名。`TGO_BUNDLE`
必须是新目录。SPE 使用 host PID namespace 对齐硬件上下文 ID，采集只针对本次任务。

需要缩小搜索范围可加 `-e TGO_LAYOUT=ext-tsp`。完整搜索和独立进程验证会多次加载
模型，耗时明显长于单次 decode。若要复用现有画像，在同一 docker 命令中额外传入
`-e TGO_RUN="$PWD/runs/vllm-spe-retired-r1"`；省略 TGO_RUN 则从头采集。
手动选择可用 `TGO_BACKEND`、`TGO_EVENT`、`TGO_BOLT_MODE` 和逗号分隔的
`TGO_IMAGE_FILTER`，默认无需这些变量。

已确认的自动选库结果为 `runs/vllm-auto-tune-r1`，独立五对 decode ROI 中位数减少
4.74%（2.92%～7.56%）。相邻入口部署使用已经升级内容校验的新入口：
`runs/vllm-auto-tune-r1/portable-entry/python3.tgo`。该文件在新的同镜像容器中挂载到
`/usr/local/python3.12.13/bin/python3.tgo`，通过了实际三库加载和 token 校验。
这仍是 CPU-stub 主机路径，不代表真实 NPU 吞吐。另一轮从头采集未找到稳定收益，
程序自动选择了 baseline；不能保证每份画像都有收益。

先检查 `$TGO_BUNDLE/tune.json`。`selected=baseline` 表示未找到确认获益的布局。
如果选中了候选，`$TGO_BUNDLE/run` 是可替代 Python 解释器的新 ELF。新容器挂载
整个项目与 bundle 后，可额外只读挂载该文件到
`/usr/local/python3.12.13/bin/python3.tgo`，把官方启动脚本的 `PY` 指向新入口，
继续使用原工作负载参数。原 Python 入口不覆盖。
同内容 ELF 的容器设备号/inode 变化会触发 SHA256 校验，不要求重新优化；原路径
和整个 bundle 仍需保持可访问。
