"""串行验证多个负载；筛选按最差场景中位数，确认仍要求每对均达标。"""
from pathlib import Path
import re
import statistics
import time
import uuid
import math
from .util import read_json,write_json,sha256,IdentityError

def read_scenarios(path):
    data=read_json(path)
    if not isinstance(data,dict) or data.get('schema')!='tgo.validation-cases.v1':raise ValueError('不是tgo.validation-cases.v1场景清单')
    if set(data)-{'schema','role','cases'} or data.get('role','validation') not in ('validation','heldout'):raise ValueError('场景清单字段或role无效')
    cases=data.get('cases')
    if not isinstance(cases,list) or not cases:raise ValueError('场景列表不能为空')
    names=set()
    for case in cases:
        if not isinstance(case,dict) or set(case)-{'name','command','cwd','env','metric'}:raise ValueError('场景字段无效')
        name=case.get('name')
        if not isinstance(name,str) or not re.fullmatch(r'[^\W_][\w.-]{0,79}',name) or name in names:raise ValueError('场景name无效或重复')
        names.add(name)
        if 'command' in case and (not isinstance(case['command'],list) or not case['command'] or not all(isinstance(x,str) and x for x in case['command'])):
            raise ValueError('场景command需要非空argv字符串列表')
        if 'env' in case and (not isinstance(case['env'],dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in case['env'].items())):
            raise ValueError('场景env需要字符串映射')
        for key in ['cwd','metric']:
            if key in case and (not isinstance(case[key],str) or not case[key]):raise ValueError('场景'+key+'需要非空字符串')
    return data

def validate_scenarios(bundle,definition,pairs=5,timeout=300,metric=None):
    from .evaluate import heldout
    if not isinstance(pairs,int) or isinstance(pairs,bool) or pairs<=0 or not math.isfinite(timeout) or timeout<=0:
        raise ValueError('pairs与timeout须为有限正数')
    root=Path(bundle).resolve();built=read_json(root/'build.json');original=read_json(Path(built['source_run'])/'run.json')
    output=root/'scenario-validations'/uuid.uuid4().hex;output.mkdir(parents=True)
    write_json(output/'cases.json',definition)
    result={'schema':'tgo.validation.v1','state':'validating','equivalent':False,'case_count':len(definition['cases']),
        'requested_pairs':pairs*len(definition['cases']),'completed_pairs':0,'cases':[],'pairs':[],
        'metric':'per-scenario-paired-native-wall','evidence_directory':str(output),'case_definition_sha256':sha256(output/'cases.json'),
        'selection_metric':'worst_case_median_reduction_percent','role':definition.get('role','validation'),'started_unix':time.time()}
    write_json(output/'validation.json',result)
    try:
        for case in definition['cases']:
            name=case['name'];result['current_case']=name;write_json(output/'validation.json',result)
            try:
                evaluation=heldout(root,output/name,case.get('command',original['command']),pairs,case.get('metric',metric),
                    timeout,case.get('cwd'),case.get('env'),require_variation=result['role']=='heldout')
                validation=evaluation['validation']
                result['cases'].append({'name':name,'evaluation':str(output/name/'evaluation.json'),'validation':validation})
                result['completed_pairs']+=validation['completed_pairs']
                result['pairs'].extend([{**row,'case':name} for row in validation['pairs']])
            except IdentityError:raise
            except (RuntimeError,ValueError,OSError) as error:
                result['cases'].append({'name':name,'error':str(error),'state':'failed'})
            write_json(output/'validation.json',result)
        validations=[row['validation'] for row in result['cases'] if 'validation' in row]
        equivalent=(len(validations)==result['case_count'] and all(v['equivalent'] and v['completed_pairs']==pairs for v in validations))
        gains=[row['reduction_percent'] for row in result['pairs']]
        result.update(state='complete',equivalent=equivalent,
            median_reduction_percent=statistics.median(gains) if gains else 0.,min_reduction_percent=min(gains) if gains else 0.,
            max_reduction_percent=max(gains) if gains else 0.,
            worst_case_median_reduction_percent=min(v['median_reduction_percent'] for v in validations) if validations else 0.,
            recommendation='candidate' if equivalent and min(gains)>0 else 'retain-baseline',finished_unix=time.time())
        return result
    except BaseException as error:result.update(state='failed',error=str(error));raise
    finally:write_json(output/'validation.json',result);write_json(root/'validation.json',result)
