"""复杂Python前端负载：对象分派、图遍历、解析编译与Unicode/正则。"""
import argparse
import ast
from collections import deque
import hashlib
import json
import random
import re
import sys
import time
from tgo.roi import region,emit_result

class Node:
    __slots__=('value','children','tag')
    def __init__(self,value,children=()):self.value=value;self.children=children;self.tag=value&7
    @property
    def weight(self):return (self.value*17+self.tag)&65535
    def transform(self,x):return ((x^self.weight)*33+self.value)&0xffffffff

class ScaledNode(Node):
    __slots__=()
    def transform(self,x):return (super().transform(x)+self.weight*3)&0xffffffff

def object_graph(seed,size):
    rng=random.Random(seed);nodes=[]
    for i in range(size):
        cls=ScaledNode if i%5==0 else Node
        nodes.append(cls(rng.randrange(65536),tuple(rng.randrange(i) for _ in range(min(i,3)))))
    return nodes

def dispatch(nodes,rounds):
    total=0;counts={}
    for turn in range(rounds):
        for index,node in enumerate(nodes):
            try:
                if (node.value+turn)&63==19:raise LookupError(node.tag)
                value=node.transform(total+index)
                if node.tag in (0,3):total=(total+value)&0xffffffff
                elif node.tag<4:total=(total^value)&0xffffffff
                else:total=(total+value//(node.tag+1))&0xffffffff
                counts[node.tag]=counts.get(node.tag,0)+1
            except LookupError as error:
                total=(total+int(error.args[0]))&0xffffffff
    return [total,sorted(counts.items())]

def graph(nodes,rounds):
    total=0;histogram={}
    for turn in range(rounds):
        queue=deque([(len(nodes)-1,0)]);visited=set()
        while queue:
            index,depth=queue.popleft()
            if index in visited:continue
            visited.add(index);node=nodes[index]
            histogram[depth]=histogram.get(depth,0)+1
            total=(total+node.transform(depth+turn))&0xffffffffffffffff
            queue.extend((child,depth+1) for child in node.children)
    return [total,sorted(histogram.items())]

def parsers(documents,rounds):
    total=0;matches=0
    pattern=re.compile(r'(?P<word>[A-Za-z汉😀]+)-(?P=word)')
    for turn in range(rounds):
        for index,document in enumerate(documents):
            text=json.dumps(document,ensure_ascii=False,sort_keys=True)
            decoded=json.loads(text)
            for word in pattern.finditer(decoded['label']):matches+=len(word.group('word'))
            source='def evaluate(x):\n return '+decoded['expression']+'\n'
            tree=ast.parse(source,filename='<frontend-workload>')
            operators=sum(isinstance(n,(ast.BinOp,ast.Compare,ast.IfExp)) for n in ast.walk(tree))
            namespace={};exec(compile(tree,'<frontend-workload>','exec'),namespace)
            total=(total+namespace['evaluate'](turn+index)+operators+sum(decoded['values']))&0xffffffff
    return [total,matches]

def prepare(seed,size):
    rng=random.Random(seed)
    nodes=object_graph(seed,size)
    expressions=['(x*17+9) ^ (x>>2)','x*3 if x%7<3 else x+19','(x*x+11) & 65535','(x+5)*(x%11+1)']
    documents=[{'label':word+'-'+word,'expression':expressions[i%4],
                'values':[rng.randrange(1000) for _ in range(24)]}
               for i,word in enumerate(['alpha','汉字','😀😀','beta']*max(1,size//64))]
    return nodes,documents

def execute(context,case,rounds):
    nodes,documents=context
    result={}
    if case in ('dispatch','mixed'):result['dispatch']=dispatch(nodes,rounds)
    if case in ('graph','mixed'):result['graph']=graph(nodes,rounds*3)
    if case in ('parsers','mixed'):result['parsers']=parsers(documents,rounds)
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description='复杂Python前端负载')
    p.add_argument('--case',choices=['dispatch','graph','parsers','mixed'],default='mixed')
    p.add_argument('--seed',type=int,default=101);p.add_argument('--size',type=int,default=1024)
    p.add_argument('--rounds',type=int,default=40);p.add_argument('--repeat',type=int,default=1)
    args=p.parse_args(argv)
    if min(args.size,args.rounds,args.repeat)<=0:p.error('size、rounds和repeat必须为正数')
    context=prepare(args.seed,args.size)
    warmup=execute(context,args.case,1)
    expected=None
    for _ in range(args.repeat):
        with region('python-'+args.case):result=execute(context,args.case,args.rounds)
        digest=hashlib.sha256(json.dumps(result,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        if expected is not None and digest!=expected:raise RuntimeError('相同输入的Python语义结果不一致')
        expected=digest
    output={'workload':'python-heavy','case':args.case,'seed':args.seed,'size':args.size,
        'rounds':args.rounds,'repeat':args.repeat,'output_sha256':expected,'semantic_result':result}
    emit_result(output);print(json.dumps(output,ensure_ascii=False,sort_keys=True))
    return output

if __name__=='__main__':main()
