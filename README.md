# Trace-guided optimization (TGO)

输入负载命令，采集目标区间的画像，优化热点 ELF/DSO，输出独立的原生 ELF 入口。
原文件保持原状；新入口可以放在原入口旁边，例如 `python3.tgo`。

目前实现 Linux AArch64 / x86-64、glibc、LLVM BOLT。Go 原生代码的重排尚不支持，
会明确列出原因。不能保证所有二进制都可改写，也不能保证所有负载都有收益。

当前进展见 [状态](docs/STATUS.md) 和 [实测记录](docs/RESULTS-20261029.md)。

```sh
python3 scripts/bootstrap.py --jobs 8
python3 -m tgo doctor
python3 -m tgo tune -o runs/my-app --backend auto -- /path/to/python app.py
runs/my-app/run
```

`tune` 顺序执行采集、三种布局初筛、独立 AB/BA 确认和入口生成。仅在每一对确认
运行都正确且收益超过阈值时选择候选；否则生成调用原版的入口。`tune.json` 记录
实际选择，不能仅凭生成了一个 ELF 就判断优化有效。

想单独审查画像或候选时：

```sh
python3 -m tgo record -o runs/train --backend perf -- /path/to/program arg
python3 -m tgo inspect runs/train
python3 -m tgo optimize --run runs/train -o runs/candidate --image libpython3.12.so.1.0
python3 -m tgo validate runs/candidate --pairs 5
```

`optimize` 输出未经收益筛选的候选，默认做三对正确性/加载验证。性能建议保存在
`validation.json`。`tune` 才会根据收益选择最终入口。

## 精确选择 ROI

支持整段进程、从启动后指定的时间窗口，以及重复的显式标记。

```sh
python3 -m tgo record -o runs/window --roi window --delay 20 --duration 10 -- ./server
python3 -m tgo record -o runs/attach --backend perf --pid 1234 --duration 10
python3 -m tgo bind runs/attach -- ./server --same-arguments
```

时间单位为秒。attach 使用 perf；QEMU 必须从启动时运行，不能事后 attach。
使用 argv 启动，不隐式执行 shell。管道或重定向可显式传 `sh -c '...'`。

Python 中可直接标记任意区间；采集和生成的入口会自动提供轻量 ROI helper：

```python
from tgo.roi import region, emit_result

warmup()
with region("decode"):
    tokens = decode_and_synchronize()
emit_result({"tokens": tokens})
```

```sh
python3 -m tgo tune -o runs/decode --roi marker --metric decode -- python app.py
```

原生代码可以加载运行目录的 `runtime/libtgo_roi.so`，调用
`tgo_roi_begin()` / `tgo_roi_end()`。QEMU 也识别先前实验的 QMSTART/QMSTOP
getpid syscall 标记。C API 支持同线程嵌套。perf 的标记是整个采集组的开关，
应由同一生产线程串行发出；QEMU 的标记只选择发出标记的线程。

不改 Python 负载源码，也可显式指定函数：

```sh
python3 -m tgo record -o runs/hook --python-hook package.module:Class.method -- python app.py
```

函数 hook 会在入口脚本运行前导入该模块并包装函数。它不会自动判断某次调用是
prefill 还是 decode；有语义边界的服务应采用显式 ROI adapter。
目前 hook 面向普通同步函数；异步/生成器和特殊描述符应使用显式标记。

## 后端

| 后端 | 适合场景 | 实现与限制 |
| --- | --- | --- |
| perf | 原生低开销采样、长任务、编译器进程树、attach | 默认 cycles IP 样本；`--branch-stack` 可使用硬件分支栈；无分支栈采用 BOLT basic profile |
| SPE | ARM 分支采样 | `--backend spe`，需要内核、PMU 和权限支持；内置已验证的 perf 分支样本转换桥，事件可用 `--event` 覆盖 |
| QEMU | 无 PMU 权限、精确 ROI 路径 | TCG 插件汇总分支/连续落空路径，不把 QEMU wall time 当原生性能；native exec 子进程不在此画像范围 |

`auto` 用 `perf stat cycles:u` 探测 PMU 权限，失败后选择 QEMU。它不会因负载执行
失败而自动重跑负载。`--qemu-header` 必须与 QEMU 插件 ABI 匹配；bootstrap 下载的
头文件可通过 `TGO_QEMU_HEADER=.../deps/install/include/qemu-plugin.h` 指定。
AppImage 已内置编译好的插件和匹配 QEMU，不要求目标机安装开发头文件或编译器。

环境变量通过 `--env KEY=VALUE` 显式保存供新入口重放；当前 shell 的其余环境由
调用者提供，不会把全部环境或凭据写入产物。新入口保留固定 argv、追加的参数、
cwd 和指定环境。虚拟环境的入口路径不会提前解析为其底层 Python 路径。

## 入口与验证

```sh
python3 -m tgo tune -o runs/demo --entry /writable/venv/bin/python3.tgo \
  --entry-mode executable -- python app.py
/writable/venv/bin/python3.tgo app.py
TGO_DISABLE=1 /writable/venv/bin/python3.tgo app.py
```

默认 command 模式保存完整负载命令；executable 模式仅替换可执行入口，参数和当前
工作目录由调用者提供，可像原 Python 解释器一样运行。相邻入口是新的原生 ELF，
使用 exec 保留退出码和信号行为。它携带二进制配置，
不依赖 Python 来启动原负载。glibc audit 按完整真实路径重定向依赖，包括绝对路径
dlopen；同名但不同路径的库不会混用。每库独立目录，保留原 `$ORIGIN` 搜索语义。
DSO 本身需要兼容原 ABI；不会将任意 ET_EXEC 硬转换成 DSO。

构建时保存 SHA256/build-id，启动时核对输入/输出 ELF 的 inode、设备、大小与纳秒
mtime。文件变化时要求重建或显式 `TGO_DISABLE=1`。这些是版本漂移检查，不是防篡改
机制。已有 `LD_AUDIT` 配置会报冲突。产物绑定本机原路径，移动产物后应重新生成入口。

验证使用新进程 AB/BA 交替运行，比较退出码、确定性输出和真实映射的优化库。
`emit_result` 有输出时使用其规范化 JSON，否则比较 stdout。业务自身的正确性测试
仍需另外执行。`--metric decode` 使用命名 ROI 的时间，并尝试记录同线程用户态的
cycles、instructions、frontend stall、L1I read miss；每项都保留打开失败原因，以及
enabled/running，不能把复用计数当成无复用实测。

## 构建与分发

`scripts/bootstrap.py` 将依赖放入 `deps/`，从锁定的官方源码归档构建 LLVM/BOLT、
QEMU 和 patchelf，检查 SHA256，自动应用已复用的 AArch64 ADR/ADRP 修复。
基础环境需要 C/C++ 编译器、Python 3.10+、make、patch、pkg-config 和可用网络。
缺失的 zlib、libffi、GLib、libelf 会按依赖顺序构建到私有 prefix。构建前端
cmake/ninja/meson 使用固定版本的 Python wheel；归档和工具身份写入清单。
perf 的构建还需要基础开发环境中的 flex、bison、m4。

```sh
python3 scripts/bootstrap.py --components llvm qemu patchelf python --jobs 8
export TGO_QEMU_HEADER="$PWD/deps/install/include/qemu-plugin.h"
python3 scripts/package_appimage.py --python deps/install/bin/python3.12
dist/tgo-0.1.0-aarch64.AppImage doctor
# 无 FUSE 环境
APPIMAGE_EXTRACT_AND_RUN=1 dist/tgo-0.1.0-aarch64.AppImage doctor
```

每个 ISA 单独打包。AppImage 携带工具、Python 标准库及非 glibc 动态依赖；最低 glibc
版本由构建机决定并写入 `package-manifest.json`。需要覆盖较老系统时，应在对应最低
版本容器构建。AppImage 不绕过 PMU 权限或容器隔离。详细设计见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，负载协议见
[docs/WORKLOADS.md](docs/WORKLOADS.md)。
