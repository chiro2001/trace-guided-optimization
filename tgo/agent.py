"""标准库实现的MCP stdio接口；长任务立即返回持久任务目录。"""
import json
import math
from pathlib import Path
import sys
from . import __version__

PROTOCOL='2025-06-18'

def schema(properties=None,required=()):
    return {'type':'object','properties':properties or {},'required':list(required),'additionalProperties':False}

STRING={'type':'string','minLength':1}
TOOLS=[
    {'name':'tgo_doctor','description':'探测优化工具、架构和PMU权限，不修改系统配置','inputSchema':schema({'perf':STRING}),'annotations':{'readOnlyHint':True}},
    {'name':'tgo_capabilities','description':'按当前ELF/运行时结构诊断离线和在线候选资格；不计算或比较已知应用SHA名单',
     'inputSchema':schema({'image':STRING},['image']),'annotations':{'readOnlyHint':True}},
    {'name':'tgo_brbe_probe_start','description':'启动标准perf BRBE实机探测任务；采样真实用户态分支并检查质量，不修改系统或已有业务亲和性',
     'inputSchema':schema({'job_output':STRING,'output':STRING,'perf':STRING,'cpus':STRING,'event':STRING,
        'frequency':{'type':'integer','minimum':1},'duration':{'type':'number','minimum':.1,'maximum':30},
        'mmap_pages':{'type':'integer','minimum':1,'maximum':65536},
        'cwd':STRING},['job_output','output']),
     'annotations':{'readOnlyHint':False,'destructiveHint':False}},
    {'name':'tgo_brbe_record_start','description':'启动BRBE业务采集任务；command启动或pid附加二选一，显式BRBE不回退，默认继承负载亲和性',
     'inputSchema':schema({'job_output':STRING,'output':STRING,'perf':STRING,'command':{'type':'array','items':STRING,'minItems':1},
        'pid':{'type':'integer','minimum':1},'cpus':STRING,'event':STRING,
        'roi':{'type':'string','enum':['whole','marker','window']},
        'duration':{'type':'number','exclusiveMinimum':0},'delay':{'type':'number','minimum':0},
        'frequency':{'type':'integer','minimum':1},'period':{'type':'integer','minimum':1},
        'timeout':{'type':'number','exclusiveMinimum':0},'mmap_pages':{'type':'integer','minimum':1,'maximum':65536},'cwd':STRING},['job_output','output']),
     'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':True}},
    {'name':'tgo_job_start','description':'用argv启动自有调优任务；无隐式shell，-m tgo自动冻结源码，返回可查询和取消的任务目录',
     'inputSchema':schema({'command':{'type':'array','items':STRING,'minItems':1},'output':STRING,'cwd':STRING,
        'env':{'type':'object','additionalProperties':{'type':'string'}},'timeout':{'type':'number','exclusiveMinimum':0},
        'mem_floor_gib':{'type':'number','exclusiveMinimum':0},'wait_for':{'type':'array','items':STRING,'minItems':1}},['command','output']),
     'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':True}},
    {'name':'tgo_job_status','description':'读取任务状态，可选读取有限长度的原始日志尾部',
     'inputSchema':schema({'directory':STRING,'log_tail_bytes':{'type':'integer','minimum':0,'maximum':32768}},['directory']),
     'annotations':{'readOnlyHint':True}},
    {'name':'tgo_job_stop','description':'取消本工具启动的任务；核验监督器PID、启动时间、UID和命令身份',
     'inputSchema':schema({'directory':STRING},['directory']),'annotations':{'readOnlyHint':False,'destructiveHint':False}},
    {'name':'tgo_manifest_read','description':'读取有tgo.schema标识的JSON证据清单，单文件不超过2MB',
     'inputSchema':schema({'path':STRING},['path']),'annotations':{'readOnlyHint':True}},
    {'name':'tgo_live_python_start','description':'启动自有长驻Python在线验证或调优任务；mode=tune执行布局筛选/独立确认/新seed留出，各会话同PID/原对象/AB/BA/回滚',
     'inputSchema':schema({'job_output':STRING,'output':STRING,'bundle':STRING,'image':STRING,'bolt':STRING,'python':STRING,
        'cpus':STRING,'case':{'type':'string','enum':['dispatch','graph','parsers','mixed']},
        'workload':{'type':'string','enum':['python-heavy','python-stdlib']},'modules':{'type':'integer','minimum':1},
        'seed':{'type':'integer'},'size':{'type':'integer','minimum':1},'rounds':{'type':'integer','minimum':1},
        'pairs':{'type':'integer','minimum':1},'warmups':{'type':'integer','minimum':0},
        'mode':{'type':'string','enum':['replay','tune']},'capsule':{'type':'boolean'},'lite':{'type':'boolean'},
        'layout':{'type':'string','enum':['ext-tsp','compact','aligned']},
        'optimization_level':{'type':'string','enum':['conservative','balanced','aggressive']},
        'function_order':{'type':'string','enum':['cdsort','hfsort+','none']},
        'heldout_seed':{'type':'integer'},'search_pairs':{'type':'integer','minimum':1},
        'minimum_gain':{'type':'number','minimum':0},'max_heldout_regression':{'type':'number','minimum':0},
        'confirm_candidates':{'type':'integer','minimum':1},
        'variant':{'type':'array','items':{'type':'string','enum':['ext-tsp','aligned','compact','balanced','lite']},'minItems':1},
        'timeout':{'type':'number','exclusiveMinimum':0},'cwd':STRING,
        'wait_for':{'type':'array','items':STRING,'minItems':1}},['job_output','output','bundle','image','bolt','python']),
     'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':True}},
]

def validate_arguments(value,definition):
    if not isinstance(value,dict):raise ValueError('arguments必须为对象')
    properties=definition['properties']
    if set(value)-set(properties):raise ValueError('未知参数:'+','.join(sorted(set(value)-set(properties))))
    if set(definition['required'])-set(value):raise ValueError('缺少参数:'+','.join(sorted(set(definition['required'])-set(value))))
    for key,item in value.items():
        rule=properties[key];kind=rule['type']
        valid=(isinstance(item,str) and bool(item)) if kind=='string' else \
              (isinstance(item,list) and bool(item) and all(isinstance(x,str) and bool(x) for x in item)) if kind=='array' else \
              (isinstance(item,dict) and all(isinstance(k,str) and isinstance(v,str) for k,v in item.items())) if kind=='object' else \
              isinstance(item,bool) if kind=='boolean' else \
              (isinstance(item,int) and not isinstance(item,bool)) if kind=='integer' else \
              (isinstance(item,(int,float)) and not isinstance(item,bool))
        if not valid:raise ValueError('参数类型错误:'+key)
        if 'enum' in rule and item not in rule['enum']:raise ValueError('参数不在可用值中:'+key)
        if kind=='array' and 'enum' in rule['items'] and any(x not in rule['items']['enum'] for x in item):raise ValueError('数组参数不在可用值中:'+key)
        if kind in ('integer','number') and isinstance(item,float) and not math.isfinite(item):raise ValueError('数值必须有限:'+key)
        if 'minimum' in rule and item<rule['minimum']:raise ValueError('参数低于最小值:'+key)
        if 'maximum' in rule and item>rule['maximum']:raise ValueError('参数超过最大值:'+key)
        if 'exclusiveMinimum' in rule and item<=rule['exclusiveMinimum']:raise ValueError('参数必须为正数:'+key)

def call(name,arguments):
    definition=next((t for t in TOOLS if t['name']==name),None)
    if definition is None:raise ValueError('未知工具:'+str(name))
    validate_arguments(arguments,definition['inputSchema'])
    if name=='tgo_doctor':
        from .collect import doctor
        return doctor(arguments.get('perf'))
    if name=='tgo_capabilities':
        from .capabilities import inspect_image
        return inspect_image(arguments['image'])
    if name in ('tgo_brbe_probe_start','tgo_brbe_record_start'):
        from .jobs import launch
        from .brbe import record_options
        record_options(arguments.get('mmap_pages',128))
        probe=name=='tgo_brbe_probe_start'
        if probe:
            command=[sys.executable,'-m','tgo','brbe','probe']
        else:
            if bool(arguments.get('command'))==bool(arguments.get('pid')):raise ValueError('command 和 pid 必须二选一')
            if arguments.get('pid') and ('cpus' in arguments or arguments.get('roi','whole')=='marker'):
                raise ValueError('attach 不接受 cpus/marker，不修改已有业务亲和性')
            if (arguments.get('pid') or arguments.get('roi')=='window') and 'duration' not in arguments:
                raise ValueError('attach/window 需要 duration')
            if 'period' in arguments and 'frequency' in arguments:raise ValueError('period 和 frequency 二选一')
            command=[sys.executable,'-m','tgo','record','--backend','brbe']
        for key in ['output','perf','cpus','event','frequency','period','duration','pid','roi','delay','timeout']:
            if key in arguments:command+=['--'+key,str(arguments[key])]
        if 'mmap_pages' in arguments:command+=['--mmap-pages' if probe else '--brbe-mmap-pages',str(arguments['mmap_pages'])]
        if arguments.get('command'):command+=['--',*arguments['command']]
        return launch(command,arguments['job_output'],arguments.get('cwd'),timeout=arguments.get('timeout',60 if probe else 600)+30)
    if name=='tgo_job_start':
        from .jobs import launch
        return launch(arguments['command'],arguments['output'],arguments.get('cwd'),arguments.get('env'),
            arguments.get('timeout'),mem_floor_gib=arguments.get('mem_floor_gib'),wait_for=arguments.get('wait_for'))
    if name=='tgo_live_python_start':
        from .jobs import launch
        tuning=arguments.get('mode','replay')=='tune'
        stdlib=arguments.get('workload','python-heavy')=='python-stdlib'
        tune_keys={'heldout_seed','search_pairs','minimum_gain','max_heldout_regression','confirm_candidates','variant'}
        replay_keys={'layout','optimization_level','lite'}
        incompatible=set(arguments)&(replay_keys if tuning else tune_keys)
        incompatible |= set(arguments)&({'case','size'} if stdlib else {'modules'})
        if incompatible:raise ValueError('该mode不接受参数:'+','.join(sorted(incompatible)))
        action=('tune-stdlib' if tuning else 'stdlib') if stdlib else ('tune-python' if tuning else 'python')
        command=[sys.executable,'-m','tgo','live',action]
        for key in ['output','bundle','image','bolt','python','cpus','case','seed','size','rounds','pairs','warmups',
                    'layout','optimization_level','function_order','modules','heldout_seed','search_pairs','minimum_gain',
                    'max_heldout_regression','confirm_candidates']:
            if key in arguments:command+=['--'+key.replace('_','-'),str(arguments[key])]
        for key in ['capsule','lite']:
            if arguments.get(key):command+=['--'+key]
        for variant in arguments.get('variant',[]):command+=['--variant',variant]
        return launch(command,arguments['job_output'],arguments.get('cwd'),timeout=arguments.get('timeout',1800),
            wait_for=arguments.get('wait_for'))
    if name=='tgo_job_stop':
        from .jobs import stop
        return stop(arguments['directory'])
    if name=='tgo_job_status':
        from .jobs import state
        result=dict(state(arguments['directory']));size=arguments.get('log_tail_bytes',0)
        if size:
            result['log_tails']={}
            for filename in ['stdout.log','stderr.log','supervisor.log']:
                path=Path(arguments['directory'])/filename
                if path.is_file():
                    with path.open('rb') as handle:
                        handle.seek(max(0,path.stat().st_size-size));result['log_tails'][filename]=handle.read(size).decode(errors='replace')
        return result
    if name=='tgo_manifest_read':
        path=Path(arguments['path'])
        if not path.is_file() or path.stat().st_size>2000000:raise ValueError('清单必须为不超过2MB的普通文件')
        result=json.loads(path.read_text())
        if not isinstance(result,dict) or not str(result.get('schema','')).startswith('tgo.'):raise ValueError('不是TGO类型清单')
        return result
    raise ValueError('工具未实现')

def dispatch(request):
    if not isinstance(request,dict) or request.get('jsonrpc')!='2.0' or not isinstance(request.get('method'),str):
        return {'jsonrpc':'2.0','id':request.get('id') if isinstance(request,dict) else None,
            'error':{'code':-32600,'message':'Invalid Request'}}
    if 'id' not in request:return None
    identity=request['id'];method=request['method'];params=request.get('params',{})
    if not isinstance(params,dict):return {'jsonrpc':'2.0','id':identity,'error':{'code':-32602,'message':'params必须为对象'}}
    if not (identity is None or isinstance(identity,str) or isinstance(identity,int) and not isinstance(identity,bool) or isinstance(identity,float) and math.isfinite(identity)):
        return {'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request id'}}
    if method=='initialize':
        return {'jsonrpc':'2.0','id':identity,'result':{'protocolVersion':PROTOCOL,
            'capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'tgo','version':__version__}}}
    if method=='ping':return {'jsonrpc':'2.0','id':identity,'result':{}}
    if method=='tools/list':return {'jsonrpc':'2.0','id':identity,'result':{'tools':TOOLS}}
    if method=='tools/call':
        try:
            result=call(params.get('name'),params.get('arguments',{}))
            result={'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False,allow_nan=False)}],
                'structuredContent':result,'isError':False}
        except Exception as error:
            result={'content':[{'type':'text','text':str(error)}],'isError':True}
        return {'jsonrpc':'2.0','id':identity,'result':result}
    return {'jsonrpc':'2.0','id':identity,'error':{'code':-32601,'message':'Method not found'}}

def serve(input_stream=None,output_stream=None):
    source=input_stream or sys.stdin;target=output_stream or sys.stdout
    while True:
        line=source.readline(4*1024*1024+1)
        if not line:break
        try:
            if len(line)>4*1024*1024:raise ValueError('消息过长')
            request=json.loads(line,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('非有限JSON值:'+value)))
            response=dispatch(request)
        except (ValueError,TypeError) as error:
            response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':str(error)}}
        if response is not None:target.write(json.dumps(response,ensure_ascii=False,allow_nan=False)+'\n');target.flush()
