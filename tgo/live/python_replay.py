"""使用已有边画像，在自有长驻Python中构建、切换、独立配对与回滚。"""
from pathlib import Path
from .build import profile_input
from .native_gate import partitions
from .session import paired_replay

def options(parser,configurations=True,workload='python-heavy'):
    parser.add_argument('-o','--output',required=True)
    parser.add_argument('--bundle',required=True);parser.add_argument('--image',required=True)
    parser.add_argument('--bolt',required=True);parser.add_argument('--python',required=True)
    parser.add_argument('--cpus');parser.set_defaults(workload=workload)
    if workload=='python-heavy':
        parser.add_argument('--case',choices=['dispatch','graph','parsers','mixed'],default='mixed')
        parser.add_argument('--size',type=int,default=1024)
    else:
        parser.set_defaults(case='stdlib',size=1);parser.add_argument('--modules',type=int,default=24)
    parser.add_argument('--seed',type=int,default=202)
    parser.add_argument('--rounds',type=int,default=25 if workload=='python-stdlib' else 500);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--warmups',type=int,default=1)
    if configurations:parser.add_argument('--lite',action='store_true')
    parser.add_argument('--function-order',choices=['cdsort','hfsort+','none'])
    parser.add_argument('--capsule',action='store_true',help='使用专门在线代码包，旧代码与入口完全由运行时事务管理')
    if configurations:
        parser.add_argument('--layout',choices=['ext-tsp','compact','aligned'],default='ext-tsp')
        parser.add_argument('--optimization-level',choices=['conservative','balanced','aggressive'],default='aggressive')

def run(args):
    if min(args.size,args.rounds,args.pairs,getattr(args,'modules',1))<=0 or args.warmups<0:raise ValueError('规模、轮数和配对数须为正数，预热不可为负数')
    reference=profile_input(args.bundle,args.image)
    main,helpers=partitions(args.cpus)
    configuration={'adapter':args.workload,'image':str(Path(args.image).resolve()),
        'main_cpu':main,'helper_cpus':helpers,'case':args.case,'seed':args.seed,'size':args.size,'rounds':args.rounds}
    if args.workload=='python-stdlib':configuration['modules']=args.modules
    return paired_replay(reference,args.output,configuration,args.python,args.bolt,args.pairs,args.warmups,args.lite,args.function_order,args.capsule,
                         args.layout,args.optimization_level)
