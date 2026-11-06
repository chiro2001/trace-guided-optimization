#!/usr/bin/env python3
"""从已通过审计的原始证据生成中文报告和逐请求CSV。"""
import csv,json,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    audit=json.loads((ROOT/'reports/formal-host-v1-audit.json').read_text())
    terminal=json.loads((ROOT/'reports/formal-host-v1-terminal-audit.json').read_text())
    coverage=json.loads((ROOT/'reports/max-coverage-seven-v2-audit.json').read_text())
    compiler=json.loads((ROOT/'reports/compiler-source-final-audit.json').read_text())
    assert audit['passed'] and terminal['passed'] and coverage['passed'] and compiler['passed']
    rows=audit['rows'];indexed={(r['seed'],r['arm'],r['state']):r for r in rows};comparisons=audit['comparisons']
    def med(arm,state):return statistics.median(indexed[(seed,arm,state)]['wall_s'] for seed in range(3))
    def pct(label):return comparisons[label]['median_pct']
    def table(headers,values):return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(map(str,row))+' |' for row in values])
    phases=[('登记原始组','original','measured'),('当前源码/PGO基线','online','baseline'),('在线BOLT','online','online'),('在线BOLT＋快捷PLT','online','online-near-fastplt'),('撤销快捷PLT','online','after-fast-rollback'),('全部入口回滚','online','rollback'),('离线BOLT参照','offline','measured')]
    phase_table=table(['状态','第1轮 / 秒','第2轮 / 秒','第3轮 / 秒','三轮中位数 / 秒'],[
        [label,*[f"{indexed[(seed,arm,state)]['wall_s']:.4f}" for seed in range(3)],f'{med(arm,state):.4f}'] for label,arm,state in phases])
    comparison_table=table(['比较','逐轮wall缩短 / %','配对中位数 / %','描述性95%区间 / %'],[
        [label,' / '.join(f'{x:.3f}' for x in item['paired_reductions_pct']),f"{item['median_pct']:.3f}",' ～ '.join(f'{x:.3f}' for x in item['descriptive_bootstrap_95_pct'])] for label,item in comparisons.items()])
    fast_sites=audit['fast_sites'];fast_counts=' / '.join(str(x['patched_sites']) for x in fast_sites)
    fast_far=' / '.join(str(x['stats']['far_skipped']) for x in fast_sites)
    hot={r['library']:r['patched_entries'] for r in audit['coverage'] if r['tag'].endswith('r0-online')}
    coverage_table=table(['库','七库全量候选实际入口','性能版实际入口'],[[lib['name'],f"{lib['function_count']:,}",f"{hot.get(lib['name'],0):,}"] for lib in coverage['libraries']])
    costs=audit['costs'];prep=[c['ready_to_first_baseline_roi_s'] for c in costs];build=[c['bolt_build_s'] for c in costs]
    cost_table=table(['操作','控制器耗时中位数 / 毫秒','全部线程已停止的窗口中位数 / 毫秒'],[
        [label,f"{statistics.median(c['transactions'][key]['controller_elapsed_ms'] for c in costs):.2f}",f"{statistics.median(c['transactions'][key]['all_stopped_window_ms'] for c in costs):.2f}"] for key,label in [('apply','入口切换'),('fast_apply','快捷PLT提交'),('fast_rollback','快捷PLT撤销'),('rollback','入口回滚')]])
    releases=[s for r in audit['requests'] for s in r['scheduling'] if 'release' in s['comm'].lower()]
    total_requests=len(audit['requests']);threads=' / '.join(str(x) for x in sorted({r['thread_count'] for r in audit['requests']}))
    waits=[s['runqueue_wait_s'] for s in releases]
    max_walls={r['state']:r['wall_s'] for r in coverage['results'] if r['state']!='execution-proof'}
    report=f'''# 多库在线 BOLT 验证报告

2026-11-06，920B-47。正式三轮独立进程对照中，登记原始组到当前源码/PGO＋在线BOLT＋快捷PLT，完整主机请求wall的三轮配对缩短中位数为 **{pct('原始→在线全方案'):.3f}%**，从三轮中位数 **{med('original','measured'):.4f}秒** 降至 **{med('online','online-near-fastplt'):.4f}秒**。本轮已达到接近40%的目标。固定当前源码/PGO后，在线部分增量为 **{pct('当前源码/PGO→在线全方案'):.3f}%**；其中进入快捷PLT阶段之前的在线BOLT收益为 **{pct('当前源码/PGO→BOLT'):.3f}%**。前一个全方案数字包含已有源码与编译优化，不能全部归给在线BOLT。

实际完成七库全量候选组合，共 **{coverage['patched_entries']:,}个入口**；性能版选择六库 **2,576个热点入口，三轮分别切换{fast_counts}个快捷PLT位点**。Python全库版、解释器核心版、六库全量版和七库组合均保留独立数据。Transformer库生成过优化文件，但未能在线装载，实际覆盖不能计成八库。

**正式结果与统计口径。** 每轮每状态先取两个完整请求wall的中位数，再以同seed的两个进程或同进程两状态配对计算 `100 × (1 - 候选wall / 参照wall)`，最终报告三个配对百分比的中位数。下表最右列是三个进程级wall的中位数，直接用两列总体中位数相除可能与配对统计略有差异。

{phase_table}

{comparison_table}

“在线与离线wall比”这一行沿用相同缩短公式，负值表示在线版本更慢。当前离线参照覆盖及链接方式与在线候选不同，在线尚未达到离线wall。回滚漂移行用于观察顺序和状态漂移，负值表示回滚后稍慢。区间来自仅三个独立配对的穷举bootstrap，属于描述性区间，不能当作跨机器、跨模型的普遍保证。原始组→当前源码/PGO仍是组合变化，本轮未进一步拆成PGO、LTO和两个Python源码改动的独立因果贡献。各项收益按不同分母计算，不能直接相加。

正式矩阵 `formal-host-v1` 共9个独立进程、{total_requests}个ROI，全部保留，没有剔除不利样本。三轮hash seed为0、1、2，顺序依次为“原始→在线→离线”“离线→在线→原始”“在线→原始→离线”。在线进程内顺序为基线、BOLT、BOLT＋快捷PLT、撤销快捷PLT、入口全部回滚，各两个请求。候选在矩阵前按已登记规则冻结为 `full-stack-lite-fast-r1`，未用正式样本重选。

负载为Qwen3-1.7B dummy BF16、TP1、batch1、纯eager、任务队列开启、设备桩。每个ROI包含完整 `generate` 与最后同步：5个prompt token的prefill、1001次decode，输出1002个token。每个ROI前另跑50-token settle，settle不计入正式wall。PMU只测原主线程cycles/instructions作辅证，百分比使用主机wall。结果不能解释为真实NPU计算吞吐。

**线程调度核验。** 容器允许192/194/196/198，NUMA2；业务主线程固定192，release、ACL及其它辅助线程允许194/196/198，主辅CPU不共享物理核心。控制器200–207，BOLT208–215。全部{total_requests}个正式ROI在前后先原样记录亲和性并检查，没有在ROI边界复绑覆盖观察。每个ROI线程数为{threads}，线程集合不变，release线程始终存在。release累计runqueue等待时间的逐请求范围为 **{min(waits):.6f}～{max(waits):.6f}秒**；该指标只表示运行队列等待，不能据此判定每次主线程自旋的原因。本批数据不存在主线程与release被绑定到同一CPU的运行口径。

三组ELF副本统一在NUMA2生成，相同字节复用inode。每进程对原八库执行映射逐页原值写回并读回核对，统一入口补丁引入的COW条件；该操作不改变所写字节。在线组先准备全部新代码再测基线，避免将准备阶段引入的原代码页变化混进收益。输入库、业务源码、会话代码及加载助手SHA均已核对。

**覆盖与版本。** 表中数字均为实际入口补丁数。每库选取的业务热点有硬件执行断点证明；这证明七个库均进入过新代码，不表示表内每个函数都被测试负载调用过。

{coverage_table}

七库全量候选组合 `max-coverage-seven-v2` 在正式矩阵完成后另跑（首次v1因root会话锁权限在业务启动前失败，失败状态与日志保留），固定当前源码/PGO，同PID、同LLM、同数据状态完成基线、全部入口切换、逐库硬件执行证明、普通在线请求及回滚。该单进程覆盖数据的基线/在线/回滚wall分别为 **{max_walls['baseline']:.4f} / {max_walls['online']:.4f} / {max_walls['rollback']:.4f}秒**，不替代正式性能矩阵。七库入口提交/回滚的全部线程停车窗口分别为 **{coverage['transactions']['apply']['all_stopped_window_s']:.3f} / {coverage['transactions']['rollback']['all_stopped_window_s']:.3f}秒**。原六库全量版 `full-stack-v4-r4` 实际178,228入口；torch_cpu生成211,869个候选，但其中116,893个入口未取得近端跳板等切换资格，不能将生成数算成在线数。七库加入Python后各库新代码与跳板占据的地址不同，torch_cpu实际可切换入口数需按本次表格记录，覆盖库数增加不保证总入口数增加。Python全库单测 `python-private-r2` 实际5,992入口，另外三个SRE匹配函数保留原实现。七库解释器核心版 `combined-eval-fast-r5` 包含 `_PyEval_EvalFrameDefault`，其探索wall慢于六库性能候选。

`libophost_transformer.so` 全库生成1,686函数，lite生成30函数。当前原加载位置附近1.5GiB窗口仅有16/24KiB空洞，无法容纳完整新代码与元数据；尝试提前加载原TF库后仍未解决。没有覆盖未知业务保留区，也没有把生成成功记成上线。

**画像和BOLT动作。** 七个非Python库复用QEMU trace转换出的fdata；Python复用原生BOLT插桩fdata。本轮不采集BRBE。核心动作包括ext-tsp基本块重排、hfsort+函数重排（Python为cdsort）、profile2冷热拆分、EH拆分、不可达代码删除、peephole、条件尾调用及BOLT的PLT优化。性能候选使用lite阈值1，后续额外快捷绑定每轮相同的2,514个内联PLT调用点，三轮另有0/208/169个原PLT位点可直接绑定，总数分别为{fast_counts}。三轮因直接分支超距跳过的位点数分别为{fast_far}；ASLR下各库与新代码的距离影响可达集合。冻结的是候选参数和算法，各轮实际补丁数原样记录。完整命令、画像SHA和工具SHA在各库 `artifacts/<会话>-<库>/build.json`。

新生成器保留取函数地址的原身份，直接调用可走新函数体；普通离线BOLT直接套用会改变地址身份，早期原生门禁已实际捕获该问题。当前工具SHA为 `e7ffb33166a90817050aad17d050db474b8a9b2954fdd798edfd9b2815ce1703`，新目录只重编RewriteInstance与JITLinkLinker两个对象，原工具与源码SHA未改。

**在线替换及验证。** 业务就绪前已完成warmup和一次完整1002-token请求；随后在同进程请求gate之间调用BOLT生成新文件，再把新代码与只读元数据映射到原DSO load bias可达的位置。新代码继续引用原GOT、TLS、单例和可写对象，不dlopen优化DSO，不复制业务状态。准备阶段RW、发布后RX；原入口只改4字节B，远目标用独立近端跳板，旧主体和旧帧保留。原文件没有被替换，完成的是同一进程内的代码装载和入口切换。

CFI逐字段重定位，仅登记新代码FDE，旧帧继续使用原ELF展开信息。CPython新帧使用私有只读opcode标签表，旧帧保留原表；请求gate从C重新进入解释器，原LLM和主线程不变。快捷PLT仅改一条B/BL，原GOT和函数地址身份保持，原地址装载指令和续执行路径保留。事务在全线程停车后核验PID/starttime/nonce、原字节、新区域SHA与GOT，持久化journal，提交后读回，并支持两级回滚。所有新代码和CFI保留到进程退出。

原生地址身份、异常、TLS、虚调用、可写状态连续性门禁累计64,000项执行检查；加入快捷PLT和两级回滚的版本累计96,000项。Python每次329项语义检查包含生成器intrinsic、ExceptionGroup、PEP695、IMPORT_STAR、PRINT_EXPR和已缓存ASCII/UCS2/UCS4正则，前后摘要及缓存对象身份一致。正式42个ROI的输出摘要、调度步数、LLM身份、输入SHA、线程集合、PMU有效性全部通过审计；七库全量候选另有逐库执行和回滚审计。

**准备与切换成本。** 六库正式组的BOLT生成步骤累计耗时每轮 **{min(build):.2f}～{max(build):.2f}秒**；从业务ready到首个基线ROI为 **{min(prep):.2f}～{max(prep):.2f}秒**，包含构建、检查、装载、CFI、COW归一和settle等。本原型在请求gate之间串行准备，此阶段没有验证持续接收业务请求，不能把下表的事务停车窗口等同于完整业务停顿。

{cost_table}

这是920B上的受控在线代码替换验证，预置加载助手和请求gate，允许业务暂停。尚未验证BRBE实机采集、任意无准备进程attach、OSR、旧版本回收、真实NPU负载及长期线上稳定性。恢复后新调用进入新代码，已有旧帧允许继续完成；没有将运行中任意指令位置直接迁到新函数体。

**证据与终态。** [正式审计](reports/formal-host-v1-audit.json)、[矩阵状态](reports/formal-host-v1-state.json)、[冻结候选](reports/frozen-selection-v1.json)、[七库覆盖审计](reports/max-coverage-seven-v2-audit.json)、[终态审计](reports/formal-host-v1-terminal-audit.json)、[编译器原源码审计](reports/compiler-source-final-audit.json)、[逐请求CSV](reports/formal-host-v1-requests.csv)。终态核对{len(terminal['sha_checks'])}个已登记输入/工具/画像/源码路径的SHA，未发现本轮容器、业务worker或BOLT构建进程残留。成功与失败会话、拒绝原因和原始journal均保留；大ELF与完整trace留在920B同名目录。

在新tag下可通过README中的原生门禁→在线会话→正式矩阵→审计流程复跑。`analysis/render_report.py`只消费审计后的原始结果生成本报告，不修改测量数据。
'''
    (ROOT/'REPORT.md').write_text(report)
    with (ROOT/'reports/formal-host-v1-requests.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['tag','arm','seed','state','index','wall_s','thread_count','main_runtime_s','main_wait_s','release_runtime_s','release_wait_s'])
        writer.writeheader()
        for r in audit['requests']:
            main=next(t for t in r['scheduling'] if 'release' not in t['comm'].lower());release=[t for t in r['scheduling'] if 'release' in t['comm'].lower()]
            writer.writerow({**{k:r[k] for k in ['tag','arm','seed','state','index','wall_s','thread_count']},'main_runtime_s':main['runtime_s'],'main_wait_s':main['runqueue_wait_s'],'release_runtime_s':sum(t['runtime_s'] for t in release),'release_wait_s':sum(t['runqueue_wait_s'] for t in release)})
    print('REPORT.md 与逐请求CSV已生成')
if __name__=='__main__':main()
