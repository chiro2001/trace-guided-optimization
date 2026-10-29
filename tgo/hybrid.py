"""Attach independently collected counts only to byte-identical rewrite inputs."""
from copy import deepcopy
from pathlib import Path
from .capture_set import verified_sources,workload_contract
from .util import read_json,write_json,sha256,IdentityError
from .elf import ELF


def reference(run):
    root=Path(run).resolve()
    return {'run':str(root),'run_sha256':sha256(root/'run.json'),'profile_sha256':sha256(root/'profile.json')}


def verify_reference(ref):
    from .capture_set import SCHEMA,check_file
    root=Path(ref['run'])
    check_file(root/'run.json',ref['run_sha256']);check_file(root/'profile.json',ref['profile_sha256'])
    profile=read_json(root/'profile.json')
    if profile.get('schema')==SCHEMA:
        return verified_sources(profile)
    return verified_sources({'sources':[ref]})


def compose_profiles(baseline_capture,library_capture,image,native,output,required_images=()):
    baseline=Path(baseline_capture).resolve();source=read_json(baseline/'profile.json')
    state=read_json(baseline/'run.json');baseline_ref=reference(baseline);verify_reference(baseline_ref)
    library=Path(library_capture).resolve();library_ref=reference(library)
    [(training,library_profile)]=verify_reference(library_ref)
    if library_profile['backend']!='qemu' or library_profile.get('truncated'):
        raise ValueError('hybrid library counts require a complete, untruncated QEMU capture')
    rows=[];matched=[]
    total=source.get('instructions',0)
    for original in source['images']:
        row={key:value for key,value in original.items() if key not in (
            'perf_data','preaggregate','preaggregate_sha256','observations','dynamic_instructions')}
        row['sample_percent']=original.get('sample_percent',100*original.get('dynamic_instructions',0)/total if total else 0)
        row['selection_weight_source']='baseline discovery'
        if original['path']==image['original']:
            row.update(image['output'])
            row.update(native_fdata=native['profile'],native_fdata_sha256=native['sha256'],
                native_profile=str(Path(native['profile']).parent/'profile.json'),
                native_profile_sha256=sha256(Path(native['profile']).parent/'profile.json'))
        else:
            matches=[r for r in library_profile['images'] if r['sha256']==original['sha256'] and Path(r['path']).name==Path(original['path']).name]
            if len(matches)>1:raise IdentityError('ambiguous QEMU input identity')
            if matches:
                observed=matches[0]
                row.update(profile_backend='qemu',preaggregate=observed['preaggregate'],
                    preaggregate_sha256=observed['preaggregate_sha256'],profile_source_run=str(library))
                matched.append(original['path'])
            else:row['profile_unavailable']=True
        rows.append(row)
    unavailable={Path(row['path']).name for row in rows if row.get('profile_unavailable')}
    if unavailable.intersection(required_images):
        raise ValueError('required recipe inputs have no matching QEMU counts: '+', '.join(sorted(unavailable.intersection(required_images))))
    target=Path(output);target.mkdir(exist_ok=False)
    replay=deepcopy(state)
    replay.update(state='recorded',phase='complete',backend='hybrid',aggregate=True,
        sampling={'backend':'hybrid','event':None,'period':None,'frequency':None,'branch_stack':False},
        training_images=[image],collector_command=None,pid=None,
        source_runs=len(native['trials'])+1,automatic_backend={'selected':'hybrid',
        'reason':'native Python edges and independently normalized QEMU library counts'})
    a,b=workload_contract(state),workload_contract(training)
    result={'schema':'tgo.hybrid-profile.v1','backend':'hybrid','images':rows,
        'baseline_capture_ref':baseline_ref,'library_capture_ref':library_ref,
        'quality':{'kind':'independent per-object profiles','hotspot_weight':'baseline discovery shares',
            'native_python_roi_profiles':len(native['roi_profiles']),'qemu_libraries':matched,
            'library_training_variations':{key:{'replay':a[key],'training':b[key]} for key in a if a[key]!=b[key]},
            'count_policy':'native edges and QEMU counts are never added to sampling units'}}
    write_json(target/'run.json',replay);write_json(target/'profile.json',result)
    return result


def attach_library_capture(profile_path, source_run, state):
    path=Path(profile_path);source=Path(source_run).resolve()
    ref={'run':str(source),'run_sha256':sha256(source/'run.json'),'profile_sha256':sha256(source/'profile.json')}
    [(captured,library)]=verified_sources({'sources':[ref]})
    if library['backend']!='qemu' or library.get('truncated'):
        raise ValueError('hybrid library profiles need a complete, untruncated QEMU capture')
    result=read_json(path);used=[]
    for row in result['images']:
        if Path(row['path']).name.startswith('libpython'):
            continue
        matches=[r for r in library['images'] if r['sha256']==row['sha256'] and Path(r['path']).name==Path(row['path']).name]
        if len(matches)>1:raise IdentityError('ambiguous matching QEMU input identity')
        if matches:
            original=matches[0]
            row.update(profile_backend='qemu',preaggregate=original['preaggregate'],
                preaggregate_sha256=original['preaggregate_sha256'],profile_source_run=str(source))
            used.append({'path':row['path'],'sha256':row['sha256']})
    if not used:raise ValueError('QEMU library capture has no byte-identical inputs')
    a,b=workload_contract(state),workload_contract(captured)
    result.update(library_capture_ref=ref,library_profile_inputs=used,
        library_training_variations={key:{'replay':a[key],'training':b[key]} for key in a if a[key]!=b[key]})
    write_json(path,result)
    return result
