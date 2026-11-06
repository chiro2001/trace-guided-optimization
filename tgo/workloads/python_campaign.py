"""短训练、长原生确认、不同seed留出，冻结后不再用留出数据选型。"""
import argparse
from pathlib import Path
import json
import os
import time
from tgo.cli import main as tgo
from tgo.util import write_json,tool

def run(args):
    workload=Path(__file__).with_name('python_heavy.py')
    def command(rounds,seed):return [args.python,str(workload),'--case',args.case,'--rounds',str(rounds),'--seed',str(seed),'--size',str(args.size)]
    return campaign(args,command,'python-'+args.case)

def campaign(args,command,metric,schema='tgo.python-campaign.v1'):
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
    common=['--cpus',args.cpus] if args.cpus else []
    phases=[('training',['record','-o',str(root/'capture'),'--backend',args.backend,'--roi','marker',
        '--max-insns',str(args.max_insns),'--timeout',str(args.timeout),*common,
        *(['--qemu',args.qemu] if args.qemu else []),'--',*command(args.train_rounds,args.seed)]),
        ('bind-confirmation',['bind',str(root/'capture'),'--',*command(args.rounds,args.seed)]),
        ('screen-and-confirm',['tune','--run',str(root/'capture'),'-o',str(root/'tuned'),
            '--strategy','bolt','--recipe','generic','--top',str(args.top),'--entry-mode','executable',
            '--search-pairs',str(args.search_pairs),'--validate',str(args.pairs),'--metric',metric,'--timeout',str(args.timeout),
            *sum((['--layout',layout] for layout in args.layout),[]),
            *sum((['--image',image] for image in args.image),[])]),
        ('heldout',['evaluate','--bundle',str(root/'tuned'),'-o',str(root/'heldout'),'--pairs',str(args.pairs),
            '--metric',metric,'--timeout',str(args.timeout),'--',*command(args.rounds,args.heldout_seed)])]
    state={'schema':schema,'state':'running','configuration':vars(args),'phases':[],'started_unix':time.time()}
    write_json(root/'campaign.json',state)
    try:
        for label,argv in phases:
            state['current_phase']=label;write_json(root/'campaign.json',state)
            code=tgo(argv);state['phases'].append({'phase':label,'returncode':code})
            if code:raise RuntimeError('Python工作集阶段失败:'+label)
        selected=json.loads((root/'tuned/tune.json').read_text())
        heldout=json.loads((root/'heldout/evaluation.json').read_text())
        from tgo.runtime import build_runtime,make_launcher,publication_environment
        built=json.loads((root/'tuned/build.json').read_text())
        captured=json.loads((root/'capture/run.json').read_text())
        validated=heldout['equivalent'] and heldout['validation']['min_reduction_percent']>=-args.max_heldout_regression
        images=built['images'] if validated else []
        runtime=build_runtime(root/'publication')
        affinity=getattr(args,'publish_affinity','inherit')
        entry=make_launcher(runtime,root/'run',captured['command'][:1],images,publication_environment(captured['env'],affinity),None)
        state.update(state='complete',selected=selected['selected'],published=selected['selected'] if images else 'baseline',
            published_images=[image['optimized'] for image in images],entry=entry,
            published_affinity=affinity,publication_reason='semantic-and-heldout-regression-gate' if images else 'baseline or heldout gate rejected candidate',
            confirmation=selected.get('confirmation'),heldout=heldout)
    except BaseException as error:state.update(state='failed',error=str(error));raise
    finally:state['finished_unix']=time.time();write_json(root/'campaign.json',state)
    return state

def options(parser,cases=('dispatch','graph','parsers','mixed'),default='mixed'):
    parser.add_argument('-o','--output',required=True);parser.add_argument('--python',required=True)
    if cases is None:parser.set_defaults(case=default,size=1024)
    else:
        parser.add_argument('--case',choices=cases,default=default)
        parser.add_argument('--size',type=int,default=1024)
    parser.add_argument('--train-rounds',type=int,default=10)
    parser.add_argument('--rounds',type=int,default=500);parser.add_argument('--seed',type=int,default=101)
    parser.add_argument('--heldout-seed',type=int,default=202);parser.add_argument('--cpus')
    parser.add_argument('--backend',choices=['qemu','perf','auto'],default='qemu');parser.add_argument('--qemu')
    parser.add_argument('--max-insns',type=int,default=2000000000);parser.add_argument('--timeout',type=float,default=600)
    parser.add_argument('--top',type=int,default=3);parser.add_argument('--image',action='append',default=[])
    parser.add_argument('--layout',choices=['ext-tsp','compact','aligned'],action='append',default=[])
    parser.add_argument('--search-pairs',type=int,default=2);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--max-heldout-regression',type=float,default=1.0)
    parser.add_argument('--publish-affinity',choices=['inherit','workload'],default='inherit')

if __name__=='__main__':
    parser=argparse.ArgumentParser();options(parser);print(json.dumps(run(parser.parse_args()),ensure_ascii=False,indent=2))
