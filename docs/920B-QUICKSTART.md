# 920B-47 快速运行

下面使用本轮 0.2.0 AppImage，不需要安装额外 Python 包或 C 编译器。

```sh
cd ~/projects/trace-guided-optimization
TGO_APP="$PWD/dist/0.2.0/tgo-0.2.0-aarch64.AppImage"
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
`--image libpython3.12.so.1.0`。发行版解释器若没有匹配符号，会明确跳过。

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
TGO_RUN="$PWD/runs/my-vllm-spe"
TGO_BUNDLE="$PWD/runs/my-vllm-tuned"
TGO_ACTION=record

sudo -n docker run --rm --pid host --network none \
  --cpuset-cpus 216,218,220,222 --cpuset-mems 0-3 \
  --memory 24g --memory-swap 24g --pids-limit 2048 \
  --cap-add SYS_ADMIN --cap-add IPC_LOCK --security-opt seccomp=unconfined \
  -v "$TGO_ROOT:$TGO_ROOT" -v "$HOME/models:/models:ro" \
  -e PY="$TGO_ROOT/benchmarks/container_entry.sh" \
  -e TGO_APP="$TGO_APP" -e APPIMAGE_EXTRACT_AND_RUN=1 \
  -e TGO_RUN="$TGO_RUN" -e TGO_BUNDLE="$TGO_BUNDLE" \
  -e TGO_CONTAINER_ACTION="$TGO_ACTION" -e TGO_BACKEND=spe \
  -e VLLM_ENABLE_V1_MULTIPROCESSING=0 -e OMP_NUM_THREADS=1 \
  -e TASK_QUEUE_ENABLE=1 -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -e XDG_CACHE_HOME="$TGO_ROOT/build/vllm-demo-cache" \
  --entrypoint /bin/bash \
  sha256:a6314dddb8665748c456a4ba3aee5ffd985e366689327889b9b072d4ce18bf97 \
  /opt/va26-scripts/run_real_test_official.sh \
  "$TGO_ROOT/benchmarks/vllm_decode.py" --requests 16 --warmup-requests 1 --roi-cpu 216
```

先完成 `record`，然后把 `TGO_ACTION` 改为 `tune` 重复同一命令。
`TGO_RUN` 保持原采集路径，`TGO_BUNDLE` 必须是尚不存在的新目录。
SPE 使用 host PID namespace 让硬件上下文 ID 和采集侧进程 ID 对齐；采集只针对
本次启动的任务。普通 IP 采样可把 `TGO_BACKEND` 改为 `perf`。
可通过 `TGO_EVENT=arm_spe_0/ts_enable=1,branch_filter=1,event_filter=0x2,jitter=1/u`
选择本机的 retired-branch 事件；在 docker 命令中用 `-e TGO_EVENT=...` 传入。
优化阶段 `TGO_BOLT_MODE=recover` 选择独立 recovery BOLT，
`TGO_ALLOW_STRIPPED=1` 显式允许尝试缺少 `.symtab` 的库。

本轮通过独立五对确认的组合是 libpython、libtorch_npu、libtorch_python。
复用 retired SPE 采集后，在同一 docker 命令中设置以下环境；新增输出目录不能存在：

```sh
-e TGO_CONTAINER_ACTION=optimize -e TGO_BOLT_MODE=recover -e TGO_ALLOW_STRIPPED=1 \
-e TGO_IMAGE_FILTER=libpython3.12.so.1.0,libtorch_npu.so,libtorch_python.so \
-e TGO_VALIDATE_PAIRS=5
```

本机已经确认的入口为 `runs/vllm-spe-retired-multi-r1/run`，需在相同容器环境中使用。
该轮 decode ROI 中位数减少 3.68%，不代表真实 NPU 服务吞吐。

先检查 `$TGO_BUNDLE/tune.json`。`selected=baseline` 表示未找到确认获益的布局。
如果选中了候选，`$TGO_BUNDLE/run` 是可替代 Python 解释器的新 ELF。新容器挂载
整个项目与 bundle 后，可额外只读挂载该文件到
`/usr/local/python3.12.13/bin/python3.tgo`，把官方启动脚本的 `PY` 指向新入口，
继续使用原工作负载参数。原 Python 入口不覆盖。
