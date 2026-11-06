"""复用已选定工件，对未用于训练/筛选的负载独立验证。"""
from copy import deepcopy
from pathlib import Path
from .tune import compose_bundle
from .util import read_json,write_json,command_path,sha256,IdentityError,workload_env
from .validate import validate

def heldout(bundle,output,command,pairs=3,metric=None,timeout=300,cwd=None,env=None,require_variation=True):
    source=Path(bundle).resolve();built=read_json(source/'build.json')
    go_compiler=built.get('go_compiler')
    if go_compiler and not built.get('replay_env'):raise ValueError('该Go工件缺少可重放的Go-aware适配元数据，请用新版重建')
    for path,expected in built.get('replay_checks',{}).items():
        if sha256(path)!=expected:raise IdentityError('已确认工具链工件发生变化:'+path)
    original=read_json(Path(built['source_run'])/'run.json')
    contract=deepcopy(original)
    contract['cwd']=str(Path(cwd or original['cwd']).resolve())
    contract['env']={**original.get('env',{}),**(env or {})}
    execution_environment=workload_env();execution_environment.update(contract['env'])
    contract['command']=command_path(command,cwd=contract['cwd'],environment=execution_environment)
    changes={key:{'training':original.get(key),'heldout':contract[key]} for key in ['command','cwd','env'] if original.get(key)!=contract[key]}
    if require_variation and not changes:raise ValueError('留出负载需提供不同命令、显式环境或工作目录')
    root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False)
    capture=root/'contract';capture.mkdir();contract.update(heldout_from=str(source),state='heldout-contract')
    write_json(capture/'run.json',contract)
    evaluation=root/'bundle'
    result={'schema':'tgo.heldout.v1','state':'evaluating','source_bundle':str(source),
        'source_build_sha256':sha256(source/'build.json'),'contract_changes':changes,
        'role':'heldout' if require_variation else 'independent-validation','input_variation_detected':bool(changes),
        'optimized_images':[r['optimized'] for r in built['images']],
        'selection_policy':'只复用既定工件，留出结果不参与重新选择候选'}
    write_json(root/'evaluation.json',result)
    try:
        if go_compiler:
            from .runtime import build_runtime,make_launcher
            runtime=build_runtime(evaluation)
            entry=make_launcher(runtime,evaluation/'run',contract['command'],[],
                {**built['replay_env'],**contract['env']},contract['cwd'],check_paths=list(built.get('replay_checks',{})))
            composed={**built,'source_run':str(capture),'entry':entry,'entry_mode':'command','state':'candidate'}
            write_json(evaluation/'build.json',composed)
            result['go_compiler']=go_compiler
        else:
            composed=compose_bundle(evaluation,{'source_run':str(capture),'profile_sha256':built.get('profile_sha256'),
                'workload':{key:contract[key] for key in ['command','cwd','env']}},built['images'])
        validation=validate(evaluation,pairs,timeout,metric)
        result.update(state='complete',equivalent=validation['equivalent'],validation=validation)
        composed.update(validation=validation,state='validated' if validation['equivalent'] else 'rejected')
        write_json(evaluation/'build.json',composed)
        if not validation['equivalent']:Path(composed['entry']).unlink(missing_ok=True)
    except BaseException as error:
        result.update(state='failed',error=str(error));raise
    finally:write_json(root/'evaluation.json',result)
    return result
