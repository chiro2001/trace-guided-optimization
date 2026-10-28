# 第三方构建组件

官方来源和归档 SHA256 在 `dependencies.lock.json`；构建日志与产物哈希在 `deps/`。
分发时须同时遵守所携带组件各自的许可，保留对应版权及许可文件。

| 组件 | 上游 | 许可概况 |
| --- | --- | --- |
| LLVM / BOLT | llvm/llvm-project | Apache-2.0 WITH LLVM-exception |
| QEMU | qemu.org | GPL-2.0，部分文件另有兼容许可 |
| CPython | python.org | Python Software Foundation 许可及附带组件许可 |
| GLib（QEMU 构建依赖） | GNOME | LGPL-2.1-or-later |
| patchelf | NixOS/patchelf | GPL-3.0-or-later |
| AppImage 工具/runtime | AppImage | 以所选 release 的许可文件为准 |
| perf（可选） | Linux kernel tools/perf | 以对应 Linux 源码目录许可为准 |

AppImage 构建清单记录每个实际 ELF 依赖的来源路径和 SHA；系统 glibc 不捆绑。
重新分发时应附带所采用的 GPL/LGPL 对应源码、补丁和构建说明，不能仅根据本表
判断已经满足全部许可义务。AArch64 pages 补丁来自本人的先前 BPU/vLLM 实验，
基于锁定 LLVM 22.1.0，不包含其他私有实验改动。
