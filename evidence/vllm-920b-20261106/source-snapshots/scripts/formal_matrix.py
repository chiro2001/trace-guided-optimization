#!/usr/bin/env python3
"""候选冻结后采集三组独立进程配对；所有失败与样本保留。"""
import argparse,subprocess,sys,time
from common import *

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',required=True);parser.add_argument('--candidate',required=True);a=parser.parse_args()
    source=ROOT/'runs'/a.candidate/'session.json';candidate=json.loads(source.read_text())
    if candidate['status']!='complete':raise RuntimeError('候选会话未完成')
    libraries=[x['name'] for x in candidate['loaded'] if x['function_count']>0]
    if not libraries:raise RuntimeError('没有可冻结的库')
    flags=candidate['configuration']
    orders=[['original','online','offline'],['offline','online','original'],['online','original','offline']]
    path=ROOT/'reports'/(a.tag+'-state.json')
    if path.exists():raise RuntimeError('拒绝覆盖矩阵')
    frozen={str(p.relative_to(ROOT)):sha(p) for folder in ['scripts','native','reference'] for p in (ROOT/folder).glob('*') if p.is_file()}
    state={'status':'running','candidate':a.candidate,'candidate_session_sha256':sha(source),'libraries':libraries,
        'orders':orders,'requests_per_state':2,'software_sha256':frozen,'steps':[],'started_unix':time.time()}
    atomic_json(path,state)
    for seed,order in enumerate(orders):
        for arm in order:
            if any(sha(ROOT/rel)!=digest for rel,digest in frozen.items()):raise RuntimeError('冻结代码在矩阵期间改变')
            tag=a.tag+'-r'+str(seed)+'-'+arm
            if arm=='online':
                command=[sys.executable,str(ROOT/'scripts/online_session.py'),'--tag',tag,'--seed',str(seed),
                    '--identity','--plt','--formal','--no-probe','--python-gate','--requests','2','--libraries',*libraries]
                for option in ['lite','near_fastplt','hot_entries_only','reserve_constrained_first','early_tf','python_eval_only']:
                    if flags.get(option):command+=['--'+option.replace('_','-')]
            else:
                command=[sys.executable,str(ROOT/'scripts/bench.py'),'--tag',tag,'--seed',str(seed),'--arm',arm,
                    '--requests','2','--normalize','--python-gate']
                if flags.get('early_tf'):command+=['--early-tf']
            state.update(current={'tag':tag,'arm':arm,'seed':seed,'command':command});atomic_json(path,state)
            p=subprocess.run(command)
            state['steps'].append({'tag':tag,'arm':arm,'seed':seed,'exit_code':p.returncode,'finished_unix':time.time()});atomic_json(path,state)
            if p.returncode:state['status']='failed';atomic_json(path,state);raise RuntimeError('正式进程失败:'+tag)
    state.update(status='complete',finished_unix=time.time());atomic_json(path,state)
if __name__=='__main__':main()
