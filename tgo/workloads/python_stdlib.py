"""编译真实标准库源码：AST遍历与字节码生成，训练和留出使用不同模块子集。"""
import argparse
import ast
import hashlib
import json
import marshal
from pathlib import Path
import random
import sysconfig
from tgo.roi import region,emit_result
from tgo.util import sha256

def prepare(seed,limit=24,stdlib=None):
    root=Path(stdlib or sysconfig.get_path('stdlib')).resolve()
    names=['argparse.py','ast.py','dataclasses.py','typing.py','inspect.py','pydoc.py','doctest.py','enum.py','fractions.py']
    paths=[root/name for name in names if (root/name).is_file()]
    for name in ['asyncio','email','json','unittest','concurrent/futures','html','http']:
        paths.extend(sorted((root/name).glob('*.py')))
    paths=[p for p in sorted(set(paths)) if 1000<=p.stat().st_size<=250000]
    random.Random(seed).shuffle(paths);paths=paths[:limit]
    if len(paths)<min(4,limit):raise ValueError('真实标准库源码不足，请提供--stdlib')
    documents=[]
    import tokenize
    for path in paths:
        with tokenize.open(path) as stream:source=stream.read()
        documents.append((str(path.relative_to(root)),source,sha256(path)))
    return documents

def execute(documents,rounds):
    digest=hashlib.sha256();nodes=0;code_bytes=0
    for _ in range(rounds):
        for name,source,_ in documents:
            tree=ast.parse(source,filename=name)
            histogram={}
            for node in ast.walk(tree):
                kind=type(node).__name__;histogram[kind]=histogram.get(kind,0)+1
            code=compile(tree,name,'exec',dont_inherit=True,optimize=0)
            payload=marshal.dumps(code);digest.update(payload)
            digest.update(json.dumps(histogram,sort_keys=True).encode())
            nodes+=sum(histogram.values());code_bytes+=len(payload)
    return {'digest':digest.hexdigest(),'ast_nodes':nodes,'code_bytes':code_bytes}

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--stdlib');parser.add_argument('--modules',type=int,default=24)
    parser.add_argument('--seed',type=int,default=101);parser.add_argument('--rounds',type=int,default=50)
    args=parser.parse_args(argv)
    if min(args.modules,args.rounds)<=0:parser.error('模块数和轮数须为正数')
    documents=prepare(args.seed,args.modules,args.stdlib);execute(documents,1)
    with region('python-stdlib'):result=execute(documents,args.rounds)
    output={'workload':'python-stdlib-compile','seed':args.seed,'rounds':args.rounds,
        'corpus':{name:digest for name,_,digest in documents},'result':result}
    emit_result(output);print(json.dumps(output,sort_keys=True));return output

if __name__=='__main__':main()
