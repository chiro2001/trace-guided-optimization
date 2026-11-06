#!/usr/bin/env python3
"""只读核验正式样本和冻结源码，输出进程级统计；不删除或重采样本。"""
import hashlib,itertools,json,statistics,sys
from pathlib import Path
ROOT=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]
TAG=sys.argv[2] if len(sys.argv)>2 else 'formal-host-v1'
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def require(condition,message):
    if not condition:raise RuntimeError(message)
def percentile(values,p):
    values=sorted(values);position=(len(values)-1)*p;low=int(position);high=min(low+1,len(values)-1)
    return values[low]+(values[high]-values[low])*(position-low)

def main():
    state=json.loads((ROOT/'reports'/(TAG+'-state.json')).read_text());require(state['status']=='complete','矩阵尚未完成')
    matrix=json.loads((ROOT/'inputs/matrix.json').read_text());rows=[];digests=set();coverage=[];requests=[];costs=[];fast_sites=[]
    expected=[(seed,arm) for seed,order in enumerate(state['orders']) for arm in order]
    require(len(expected)==9 and [(s['seed'],s['arm']) for s in state['steps']]==expected,'正式进程数量或顺序不符')
    require(all(s['exit_code']==0 for s in state['steps']),'正式进程存在失败')
    for step in state['steps']:
        folder=ROOT/'runs'/step['tag'];session=json.loads((folder/'session.json').read_text())
        require(session['status']=='complete',step['tag']+' 未完成')
        require(session['seed']==step['seed'],'进程seed不符')
        require(session['software_sha256']==state['software_sha256'],'进程间冻结源码不一致')
        require(session.get('cow_normalization',{}).get('code_bytes_unchanged'),step['tag']+' 缺少COW归一')
        for relative,expected in session['software_sha256'].items():
            require(sha(folder/'source-snapshot'/relative)==expected,'冻结源码SHA不符:'+step['tag']+':'+relative)
        ready=session['ready'];arm='current' if step['arm']=='online' else step['arm']
        require(set(ready['libraries'])==set(matrix[arm]['libraries']),'库输入集合不符')
        require(ready['starttime_ticks']==session['host_starttime'],'进程启动身份不符')
        require(ready['sources']==matrix[arm]['sources'],'业务源码SHA不符')
        for name,library in ready['libraries'].items():require(library['sha256']==matrix[arm]['libraries'][name]['sha256'],'库输入SHA不符')
        gates=[session[k] for k in ['python_gate_baseline','python_gate_online','python_gate_rollback'] if k in session]
        require(len(gates)==(3 if step['arm']=='online' else 1),'语义门禁状态缺失')
        require(all(g['checks']==329 and g['digest']==gates[0]['digest'] and g['cached_pattern_identities']==gates[0]['cached_pattern_identities'] for g in gates),'语义或缓存对象身份改变')
        for result in session['results']:
            require(result['tokens']==1002 and result['actual_worker_steps']==1002,'不完整ROI')
            require(result['llm_identity']==ready['llm_identity'],'LLM身份改变')
            require(result['worker_tids']==[ready['pid']],'worker不在原主线程')
            require(result['scheduled_tokens']==[5]+[1]*1001,'prefill/decode调度形态改变')
            require(result['output_sha256']==ready['full_sha256'],'输出与就绪请求不一致')
            require(result['source_sha256']==ready['sources'],'ROI业务源码改变')
            require(result['business_end_monotonic_ns']-result['business_start_monotonic_ns']==result['wall_ns'],'wall边界不一致')
            digests.add(result['output_sha256'])
            for key in ['thread_affinities_before_raw','thread_affinities_after_raw']:
                threads=result[key];main_tid=ready['pid']
                require(main_tid in {r['tid'] for r in threads},'原主线程缺失')
                require(any('release' in r['comm'].lower() for r in threads),'没有release线程原始快照')
                for thread in threads:
                    allowed={192} if thread['tid']==main_tid else {194,196,198}
                    require(bool(thread['cpus']) and set(thread['cpus'])<=allowed,'线程亲和性越界')
            require({x['tid'] for x in result['thread_affinities_before_raw']}=={x['tid'] for x in result['thread_affinities_after_raw']},'ROI线程集合改变')
            for event in result['events'].values():require(event['enabled']>0 and event['running']/event['enabled']>=.999,'PMU复用或无效')
            before={t['tid']:t for t in result['thread_affinities_before_raw']};scheduling=[]
            for thread in result['thread_affinities_after_raw']:
                if thread['tid']==ready['pid'] or 'release' in thread['comm'].lower():
                    delta=[int(a)-int(b) for a,b in zip(thread['schedstat'].split(),before[thread['tid']]['schedstat'].split())]
                    require(len(delta)==3 and all(x>=0 for x in delta),'schedstat不连续')
                    scheduling.append({'tid':thread['tid'],'comm':thread['comm'],'cpus':thread['cpus'],
                        'runtime_s':delta[0]/1e9,'runqueue_wait_s':delta[1]/1e9,'timeslices':delta[2]})
            requests.append({'tag':step['tag'],'arm':step['arm'],'seed':step['seed'],'state':result.get('state','measured'),
                'index':result['index'],'wall_s':result['wall_ns']/1e9,'thread_count':len(before),'scheduling':scheduling})
        groups={}
        for result in session['results']:groups.setdefault(result.get('state','measured'),[]).append(result)
        for name,values in groups.items():
            rows.append({'tag':step['tag'],'arm':step['arm'],'seed':step['seed'],'state':name,'requests':len(values),
                'wall_s':statistics.median(v['wall_ns'] for v in values)/1e9,
                'cycles':statistics.median(v['events']['cycles']['count'] for v in values),
                'instructions':statistics.median(v['events']['instructions']['count'] for v in values)})
        if step['arm']=='online':
            for name in ['baseline','online','online-near-fastplt','after-fast-rollback','rollback']:require(len(groups.get(name,[]))==2,'在线状态样本数错误:'+name)
            require(len(groups)==5,'在线状态集合错误')
            require([lib['name'] for lib in session['loaded']]==state['libraries'],'在线库覆盖改变')
            require(not session.get('qualification_failures'),'正式候选存在资格拒绝')
            counts=sum(lib['function_count'] for lib in session['loaded'])
            require(session['apply']['patch_count']==session['rollback']['patch_count']==counts,'入口提交回滚数量不符')
            require(session['fast_apply']['patch_count']==session['fast_rollback']['patch_count']==session['near_fastplt']['function_count'],'快捷PLT提交回滚数量不符')
            require(all(session['near_fastplt']['proof'].values()),'快捷PLT约束不满足')
            require(session['near_fastplt']['stats']['conflicts']==0,'快捷PLT位点冲突')
            fast_sites.append({'tag':step['tag'],'patched_sites':session['near_fastplt']['function_count'],'stats':session['near_fastplt']['stats']})
            for lib in session['loaded']:coverage.append({'tag':step['tag'],'library':lib['name'],'patched_entries':lib['function_count'],'generated_functions':lib.get('generated_function_count'),'isb_threads':lib['isb_other_threads']})
            cost={'tag':step['tag'],'ready_to_first_baseline_roi_s':(session['results'][0]['business_start_monotonic_ns']-ready['ready_monotonic_ns'])/1e9,
                'transactions':{},'bolt_build_s':0,'new_pages':sum(lib['new_pages'] for lib in session['loaded'])}
            for name in ['apply','fast_apply','fast_rollback','rollback']:
                transaction=session[name];events=transaction['events']
                stopped=[e['monotonic_ns'] for e in events if e['event']=='thread_stopped'];detached=[e['monotonic_ns'] for e in events if e['event']=='thread_detached']
                require(len(stopped)==len(detached) and bool(stopped),'停车与恢复事件不完整')
                cost['transactions'][name]={'controller_elapsed_ms':transaction['elapsed_ns']/1e6,'all_stopped_window_ms':(min(detached)-max(stopped))/1e6}
            for name in state['libraries']:
                build=json.loads((ROOT/'artifacts'/(step['tag']+'-'+name)/'build.json').read_text())
                require(build['source_sha256']==ready['libraries'][name]['sha256'],'生成器输入SHA不符')
                require(build['runtime_identity'] and build['exit_code']==0,'生成器身份模式或退出码错误')
                require(build['tool_sha256']=='e7ffb33166a90817050aad17d050db474b8a9b2954fdd798edfd9b2815ce1703','生成工具SHA不符')
                require(ready['ready_monotonic_ns']<build['started_monotonic_ns']<build['finished_monotonic_ns']<session['results'][0]['business_start_monotonic_ns'],'生成不在业务就绪之后、正式ROI之前')
                cost['bolt_build_s']+=(build['finished_monotonic_ns']-build['started_monotonic_ns'])/1e9
            costs.append(cost)
        else:require(len(groups['measured'])==2,'参照样本数错误')
    require(len(digests)==1,'跨臂输出摘要不一致')
    require(len(requests)==42,'正式ROI总数不是42')
    indexed={(r['seed'],r['arm'],r['state']):r for r in rows};comparisons={}
    for label,reference,candidate in [
        ('原始→在线全方案',('original','measured'),('online','online-near-fastplt')),
        ('原始→当前源码/PGO',('original','measured'),('online','baseline')),
        ('当前源码/PGO→在线全方案',('online','baseline'),('online','online-near-fastplt')),
        ('当前源码/PGO→BOLT',('online','baseline'),('online','online')),
        ('BOLT→快捷PLT增量',('online','online'),('online','online-near-fastplt')),
        ('原始→离线参照',('original','measured'),('offline','measured')),
        ('基线→全回滚漂移',('online','baseline'),('online','rollback')),
        ('BOLT→撤销快捷PLT漂移',('online','online'),('online','after-fast-rollback')),
        ('在线与离线wall比',('offline','measured'),('online','online-near-fastplt'))]:
        reductions=[]
        for seed in range(3):
            a=indexed[(seed,*reference)]['wall_s'];b=indexed[(seed,*candidate)]['wall_s'];reductions.append(100*(1-b/a))
        samples=[statistics.median(reductions[i] for i in choice) for choice in itertools.product(range(3),repeat=3)]
        comparisons[label]={'paired_reductions_pct':reductions,'median_pct':statistics.median(reductions),
            'descriptive_bootstrap_95_pct':[percentile(samples,.025),percentile(samples,.975)],'independent_pairs':3}
    output={'passed':True,'matrix':TAG,'independent_processes':len(state['steps']),'output_sha256':next(iter(digests)),
        'rows':rows,'requests':requests,'comparisons':comparisons,'coverage':coverage,'fast_sites':fast_sites,'costs':costs,
        'scope':'主机设备桩完整generate＋同步wall；3个进程配对的描述性统计，非真实NPU计算吞吐与普遍保证'}
    path=ROOT/'reports'/(TAG+'-audit.json');path.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':True,'comparisons':comparisons},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
