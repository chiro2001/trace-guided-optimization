"""在线布局筛选、全新进程独立确认、新seed留出；每个会话最终回滚退出。"""
from pathlib import Path
import time
from tgo.selection import screening_score,confirmed
from tgo.util import write_json
from .build import profile_input
from .native_gate import partitions
from .python_replay import options as replay_options
from .session import paired_replay

VARIANTS={
    'ext-tsp':{'layout':'ext-tsp','optimization_level':'aggressive','lite':False},
    'aligned':{'layout':'aligned','optimization_level':'aggressive','lite':False},
    'compact':{'layout':'compact','optimization_level':'aggressive','lite':False},
    'balanced':{'layout':'ext-tsp','optimization_level':'balanced','lite':False},
    'lite':{'layout':'ext-tsp','optimization_level':'aggressive','lite':True},
}

def options(parser,workload='python-heavy'):
    replay_options(parser,configurations=False,workload=workload);parser.set_defaults(seed=101,pairs=5)
    parser.add_argument('--heldout-seed',type=int,default=303)
    parser.add_argument('--search-pairs',type=int,default=2)
    parser.add_argument('--minimum-gain',type=float,default=.5)
    parser.add_argument('--max-heldout-regression',type=float,default=1.)
    parser.add_argument('--confirm-candidates',type=int,default=2)
    parser.add_argument('--variant',choices=list(VARIANTS),action='append',default=[])

def summary(validation):
    result={k:v for k,v in validation.items() if k!='pairs'}
    result['pairs']=[{'index':row['index'],'order':row['order'],'baseline_ns':row['baseline']['wall_ns'],
        'online_ns':row['online']['wall_ns'],'reduction_percent':row['reduction_percent']} for row in validation['pairs']]
    return result

def run(args):
    import math
    if min(args.size,args.rounds,args.pairs,args.search_pairs,args.confirm_candidates,getattr(args,'modules',1))<=0 or args.warmups<0:
        raise ValueError('规模与配对数须为正数，预热不可为负数')
    if abs(args.seed)==abs(args.heldout_seed):raise ValueError('训练和留出seed规范化后须不同')
    if not all(math.isfinite(x) and x>=0 for x in [args.minimum_gain,args.max_heldout_regression]):
        raise ValueError('收益与退化门槛须为非负有限数')
    reference=profile_input(args.bundle,args.image);main,helpers=partitions(args.cpus)
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False,mode=0o700)
    record={'schema':'tgo.live-campaign.v1','state':'screening','configuration':vars(args),'reference':reference,
        'started_unix':time.time(),'candidates':[],'confirmations':[],'selected':'baseline','recommendation':'retain-baseline',
        'policy':'仅筛选与独立确认选择布局；留出可拒绝推荐，不重新排序；每会话同PID/原对象/硬件命中/回滚'}
    def save():write_json(root/'campaign.json',record)
    def execute(path,variant,seed,pairs):
        config={'adapter':args.workload,'image':str(Path(args.image).resolve()),'main_cpu':main,'helper_cpus':helpers,
            'case':args.case,'seed':seed,'size':args.size,'rounds':args.rounds}
        if args.workload=='python-stdlib':config['modules']=args.modules
        result=paired_replay(reference,path,config,args.python,args.bolt,pairs,args.warmups,
            function_order=args.function_order,capsule=args.capsule,**VARIANTS[variant])
        return {'variant':variant,'configuration':VARIANTS[variant],'session':str(path/'session.json'),
            'validation':summary(result['validation']),'entry_count':result['loaded']['function_count'],
            'hardware_execution_proofs':result['hardware_execution_proofs']}
    save()
    try:
        for variant in dict.fromkeys(args.variant or VARIANTS):
            record['current_variant']=variant;save()
            try:result=execute(root/('screen-'+variant),variant,args.seed,args.search_pairs)
            except (ValueError,RuntimeError,OSError) as error:result={'variant':variant,'error':str(error),'state':'failed'}
            record['candidates'].append(result);save()
        viable=[(screening_score(row.get('validation',{}),args.minimum_gain),row) for row in record['candidates']]
        viable=sorted([(score,row) for score,row in viable if score is not None],key=lambda pair:pair[0],reverse=True)
        record['state']='confirming';save();chosen=None
        for _,candidate in viable[:args.confirm_candidates]:
            variant=candidate['variant'];record['current_variant']=variant;save()
            try:result=execute(root/('confirm-'+variant),variant,args.seed,args.pairs)
            except (ValueError,RuntimeError,OSError) as error:result={'variant':variant,'error':str(error),'state':'failed'}
            record['confirmations'].append(result);save()
            if confirmed(result.get('validation',{}),args.minimum_gain):chosen=variant;break
        if chosen is not None:
            record.update(state='heldout',selected_before_heldout=chosen);save()
            result=execute(root/('heldout-'+chosen),chosen,args.heldout_seed,args.pairs)
            record['heldout']=result;validation=result['validation']
            if validation['equivalent'] and validation['min_reduction_percent']>=-args.max_heldout_regression:
                record.update(selected=chosen,recommendation='validated-online-configuration')
            else:record['rejection_reason']='留出语义或退化门槛未通过；保留原版，不按留出重新选型'
        record.update(state='complete',finished_unix=time.time());save();return record
    except BaseException as error:
        record.update(state='cancelled' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error),finished_unix=time.time());save();raise
