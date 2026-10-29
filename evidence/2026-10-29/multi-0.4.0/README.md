# 多轮 ROI 训练证据

主数据在 920B-47 的 `~/projects/trace-guided-optimization`，原始 perf/QEMU 文件及
全部验证日志保留在各自 `runs/`。这里保存便于 Git 审查的小型证据。

- `packages.json` 区分每个候选包的源码 commit、AppImage SHA 和实验。当前分发是
  `dist/0.4.0/tgo-0.4.0-aarch64.AppImage`；`package-manifest.json` 包含其构建输入。
  不将早期包的结果冒充为最终包的重新采集。
- `native-*` 是当前包的普通权限 perf 三轮训练、compact 独立五对、计数逐条相加及
  PATH/PYTHONPATH 清空后的项目外入口证明。`initial-native-*` 是早期 QEMU 包的独立
  结果。合成函数刻意制造取指压力，不代表实际业务的预期收益。
- `go-smoke.json`、`go-training-run.json` 是当前包无 CC、三轮独立 cache 的 Go smoke。
  单对验证和高选择门槛用于功能验证，不作 Go 收益声明。
- `vllm-single-*` 是先前新单画像的完整布局对照；`vllm-mixed-*` 仅搜索 ext-tsp，
  显式记录 CPU216/224 训练差异，CPU224 回放。只对每轮独立五对解释性能。
  `vllm-three-*` 是修复映射后 CPU216 同配置三轮训练，完整布局选择 compact；三库
  在三种布局中的 fdata 均逐条校验为每库三份输入之和。
  所有 vLLM 结果均为固定 CPU-stub 镜像的 eager 主机 decode，不外推真实 NPU。
- `vllm-portable-entry.json` 与 `verify_vllm_portable.py` 验证新容器将已确认入口只读
  挂到原 Python 旁，实际负载在项目外临时目录运行，未调用 AppImage，三库和规范化
  token JSON SHA 相符。
- `overlay-control.json`、`overlay-no-cache.json` 与 `reproduce_overlay_perf.py` 是独立
  新容器的连续三轮短 Python 复现。统计只计算 `PERF_RECORD_MMAP` 事件；源码中的
  no-cache 分支只关闭 build-id cache 写入，保留原始 build-id。复现时可通过 `TGO_PROBE_OUTPUT`
  指定新目录，脚本拒绝覆盖已有证据。
- `vllm-initial-incomplete-mappings.json`、对应 hotspots 和 permission-failure 文件保留
  被拒绝的早期实验。它们不是多轮训练收益的证据。
- `tests-arm.log` 33 项全部通过；`tests-local.log` 33 项中一项真实 LLVM 工具测试跳过。
  初始日志与构建 manifest 以 `initial-` 前缀保留。

在主仓库执行 `python3 evidence/2026-10-29/multi-0.4.0/audit_fdata_sums.py RUN_NAME`
可重新校验已完成实验的源证据和 fdata 数值之和，输出 `runs/RUN_NAME/multi-summary.json`。
脚本支持 branch 与 no_lbr 两种实际输出。容器以 root 采集的原始文件需用相同权限
读取；不会自动更改原始文件权限。JSON summary 包含每份输入 SHA、最终计数和 PMU
口径。完整的正确性与原生性能确认仍以各轮 tune.json/validation.json 为准。
