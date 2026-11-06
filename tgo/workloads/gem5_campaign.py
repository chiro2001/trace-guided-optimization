"""gem5宿主模拟器的短画像、原生筛选/确认与新guest输入留出。"""
from pathlib import Path
from tgo.util import read_json,sha256,IdentityError
from .python_campaign import campaign

def options(parser):
    parser.add_argument('-o','--output',required=True);parser.add_argument('--gem5',required=True)
    parser.add_argument('--fixture',required=True,help='workload gem5 prepare生成的目录')
    parser.add_argument('--cpu',choices=['o3','minor','timing'],default='o3')
    parser.add_argument('--train-steps',dest='train_rounds',type=int,default=2000)
    parser.add_argument('--steps',dest='rounds',type=int,default=100000)
    parser.add_argument('--seed',type=int,default=101);parser.add_argument('--heldout-seed',type=int,default=202)
    parser.add_argument('--cpus');parser.add_argument('--backend',choices=['qemu','perf','auto'],default='qemu')
    parser.add_argument('--qemu');parser.add_argument('--max-insns',type=int,default=2000000000)
    parser.add_argument('--timeout',type=float,default=1200);parser.add_argument('--search-pairs',type=int,default=2)
    parser.add_argument('--pairs',type=int,default=5);parser.add_argument('--max-heldout-regression',type=float,default=1.)
    parser.add_argument('--layout',choices=['ext-tsp','compact','aligned'],action='append',default=[])
    parser.add_argument('--publish-affinity',choices=['inherit','workload'],default='inherit')
    parser.set_defaults(size=1,top=1)

def run(args):
    fixture=Path(args.fixture).resolve();manifest=read_json(fixture/'fixture.json')
    if manifest.get('schema')!='tgo.gem5-fixture.v1':raise ValueError('不是gem5 guest工作集目录')
    guest=Path(manifest['guest']).resolve();digest=sha256(guest)
    if digest!=manifest['guest_sha256']:raise IdentityError('本次gem5 guest在采集前发生变化')
    binary=Path(args.gem5).resolve();args.image=[str(binary)]
    config=Path(__file__).with_name('gem5_se.py')
    def command(steps,seed):
        return [str(binary),str(config),'--guest',str(guest),'--guest-sha256',digest,
                '--cpu',args.cpu,'--steps',str(steps),'--seed',str(seed)]
    return campaign(args,command,'gem5-simulate',schema='tgo.gem5-campaign.v1')
