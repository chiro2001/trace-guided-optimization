"""按运行时可用语法执行门禁，缓存对象身份在同一进程内保持。"""
import contextlib
import hashlib
import io
import json
import re

def require(value):
    if not value:raise RuntimeError('在线语义门禁断言不符')

def run(context):
    if not hasattr(context,'semantic_patterns'):
        context.semantic_patterns=[re.compile(r'(a+?)(b+)'),re.compile(r'(?P<x>汉+).*(?P=x)'),
            re.compile(r'(😀+)(?:x|y)\1'),re.compile(r'([a-z]+)-\1')]
    rows=[];checks=0;features={}
    for n in range(128):
        value=sum(i*i for i in range(n));require(value==(n-1)*n*(2*n-1)//6)
        rows.append(value);checks+=1
    class Box:
        def __init__(self,value):self.value=value
        @property
        def twice(self):return self.value*2
        def add(self,x):return self.value+x
    for n in range(32):
        item=Box(n);require(item.twice==2*n and item.add(9)==n+9)
        rows.append([item.twice,item.add(9)]);checks+=2
    def generator():
        yield 3
        raise StopIteration(17)
    item=generator();require(next(item)==3)
    try:next(item)
    except RuntimeError as error:require(isinstance(error.__cause__,StopIteration))
    else:raise AssertionError('生成器intrinsic未保留')
    checks+=2
    optional={'exception-group':'''caught=[]
try: raise ExceptionGroup('gate',[ValueError('a'),TypeError('b')])
except* ValueError as e: caught.append(('value',len(e.exceptions)))
except* TypeError as e: caught.append(('type',len(e.exceptions)))
assert caught==[('value',1),('type',1)]
result=caught
''','type-alias':'''type LabAlias[T] = tuple[T,T]
assert LabAlias.__value__.__origin__ is tuple and len(LabAlias.__type_params__)==1
result=LabAlias.__name__
'''}
    for name,source in optional.items():
        try:code=compile(source,'<live-semantic>','exec',optimize=0)
        except SyntaxError:features[name]=False;continue
        namespace={};exec(code,namespace);features[name]=True;rows.append(namespace['result']);checks+=1
    namespace={};exec('from math import *',namespace);require(namespace['sqrt'](81)==9)
    output=io.StringIO()
    with contextlib.redirect_stdout(output):exec(compile('4+5','<live-semantic>','single'),{})
    require(output.getvalue()=='9\n');rows.append(output.getvalue());checks+=2
    for pattern,text in zip(context.semantic_patterns,['aaabbbb','汉汉ab汉汉','😀😀x😀😀','word-word']):
        for _ in range(32):
            match=pattern.fullmatch(text);require(match is not None)
            rows.append((match.span(),match.groups()));checks+=1
    digest=hashlib.sha256(json.dumps([rows,features],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    result={'checks':checks,'digest':digest,'features':features,'cached_object_ids':[id(p) for p in context.semantic_patterns]}
    previous=getattr(context,'semantic_baseline',None)
    if previous is not None and result!=previous:raise RuntimeError('在线语义、可用语法或缓存对象身份改变')
    context.semantic_baseline=result
    return result
