from pathlib import Path
from .python_campaign import options as common_options,campaign

def options(parser):
    common_options(parser,cases=None,default='stdlib')
    parser.set_defaults(train_rounds=1,rounds=25)
    parser.add_argument('--stdlib');parser.add_argument('--modules',type=int,default=24)

def run(args):
    script=Path(__file__).with_name('python_stdlib.py')
    def command(rounds,seed):
        return [args.python,str(script),'--rounds',str(rounds),'--seed',str(seed),'--modules',str(args.modules),
                *(['--stdlib',args.stdlib] if args.stdlib else [])]
    return campaign(args,command,'python-stdlib')
