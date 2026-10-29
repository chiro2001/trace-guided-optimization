# vLLM 完整八库自动路线

固定 Qwen3.5-0.8B、CPU-stub 镜像、四步 eager decode、1 次 warmup、9 次测量请求。
所有百分比为 ROI 时间减少；PMU 另行列出。收益限于这份主机侧负载。

| 路线 | 独立五对 ROI 中位数 | 说明 |
| --- | --- | --- |
| 旧八库经新 TGO 入口 | 38.97% | 加载回归，原始 maps 补核五次均为八库 |
| 本轮完整二进制策略 + SPE | 14.96% | 当次 NumPy 分类尚未修复，实际七库 |
| 本轮新 ROI GCC PGO/LTO + SPE | 21.11% | 新源码编译与新训练，尚无原生 BOLT/QEMU 混合 |
| 本轮新 PGO/LTO + 原生 BOLT + 七库 QEMU | **39.29%** | 本轮构建/采集缓存复用模式，五对均正确并加载八库 |
| 0.5.0 AppImage 从头全流程 | **39.47%** | 无画像/构建复用，五对均正确并加载八库 |

混合路线的五对范围为 36.42%～40.78%，cycles 减少中位数 40.66%，指令减少
13.18%，前端 stall 减少 63.53%，L1I read miss 减少 52.24%，所有 PMU 无复用。
三轮 Python 原生训练共产生 27 个 ROI fdata；七库 QEMU 来自本轮完整画像，
353,703,301 条动态指令。每库按输入 SHA256 绑定相应画像，不相加不同计数单位。

- `intermediate.json`：对照路线、逐对 PMU 中位数、完整八库加载补核、QEMU 身份。
- `hybrid.json`：混合路线的来源、工具命令、输入/输出 SHA、27 份 ROI 与独立确认。
- `hybrid-entry.json`：新容器只读 bundle、相邻 Python ELF、无项目源码/工具挂载，
  从 `/tmp` 运行，八库全部加载、关闭优化恢复原库、输出一致。
- `package.json`：0.5.0 AppImage 身份、包内编译输入、源码提交与首版资源导出失败记录。

`fresh-appimage.json` 保存完整从头流程：关闭网络、包内源码、149 份 gcda、27 份原生
ROI fdata、单窗口完整 QEMU、逐库输入/输出、独立五对和实际 Docker 命令。
QEMU 画像为 352,496,354 条指令、1 个未截断窗口；原始 ROI 日志仍有九对 begin/end，
完整输出与发现阶段一致。五对 ROI 范围为 36.80%～42.96%，cycles 减少中位数 40.80%，
指令减少 13.18%，前端 stall 减少 63.69%，L1I read miss 减少 52.81%，PMU 无复用。
`fresh-entry.json` 保存该产物的新容器入口、八库映射及关闭优化检查，包含原始 maps
的 SHA、八库输出 SHA 和 build manifest SHA，与从头性能证据交叉核对通过。
`checks.json`、`tests-local.log`、`tests-arm.log` 保存 47 项测试记录及 SDK 跳过原因。

分发文件已回传到本地及保留在远端 `dist/0.5.0/tgo-0.5.0-aarch64.AppImage`，
SHA256：`21719e548822588e6d99d3f346072df66c8d8a5c47fd58a4494b3ac6d447cce0`。

复用和从头两条路线分开报告，不合并计数或挑选最好的一对。首版 AppImage 的资源
导出错误、原始失败证据和对应 SHA 均保留在 `package.json`。
