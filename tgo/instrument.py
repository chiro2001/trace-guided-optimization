"""Derive ROI reset/dump hooks from the actual BOLT runtime object and linked code."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

from .artifact import stage_image
from .elf import ELF, normalize_loads
from .runtime import build_runtime, make_launcher
from .util import IdentityError, read_json, write_json, run, sha256, tool, tool_env, workload_env,dependency_asset
from .validate import execute_trial


def runtime_archive():
    root = Path(__file__).resolve().parents[1]
    candidates = [os.environ.get('TGO_BOLT_RUNTIME'), dependency_asset('lib/libbolt_rt_instr.a'),
                  root/'deps/build/llvm/lib/libbolt_rt_instr.a']
    if os.environ.get('TGO_APPDIR'):
        candidates.insert(0, Path(os.environ['TGO_APPDIR'])/'usr/lib/tgo-bolt/libbolt_rt_instr.a')
    return next((Path(p).resolve() for p in candidates if p and Path(p).is_file()), None)


def archive_member(path, name):
    data=Path(path).read_bytes()
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('not a static archive')
    position=8;strings=b'';matches=[]
    while position<len(data):
        header=data[position:position+60]
        if len(header)!=60 or header[58:60]!=b'`\n':raise ValueError('truncated archive header')
        size=int(header[48:58]);label=header[:16].decode().strip()
        content=data[position+60:position+60+size]
        if len(content)!=size:raise ValueError('truncated archive member')
        if label=='//':strings=content
        elif label not in ('/','/SYM64/'):
            if label.startswith('#1/'):
                length=int(label[3:]);label=content[:length].decode().rstrip('\0');content=content[length:]
            elif label.startswith('/') and label[1:].isdigit():
                label=strings[int(label[1:]):].split(b'/\n',1)[0].decode()
            else:label=label.rstrip('/')
            if label==name:matches.append(content)
        position+=60+size+(size%2)
    if len(matches)!=1:raise ValueError('missing or duplicate archive member: '+name)
    return matches[0]


def runtime_hooks(object_path, library):
    obj, linked = ELF(object_path), ELF(library)
    if obj.machine != 183 or linked.machine != 183:
        raise ValueError('checked native BOLT hooks currently require AArch64')
    fini = obj.symbol('__bolt_instr_fini')
    anchor = linked.symbol('__bolt_runtime_fini')[1]
    base = anchor-fini[1]
    source_section = obj.section_entries[fini[0]]
    if source_section[3] != 0:
        raise ValueError('unexpected runtime object text base')
    matches = [s for s in linked.section_entries if s[3] == base and s[2] & 4 and s[5] >= source_section[5]]
    if len(matches) != 1:
        raise ValueError('runtime anchor does not identify one complete executable section')
    destination = matches[0]
    result = {'runtime_text_base':base, 'runtime_object_sha256':sha256(object_path)}
    for short, name in [('reset','__bolt_instr_clear_counters'),('dump','__bolt_instr_data_dump')]:
        section, offset, size = obj.symbol(name)
        if section != fini[0] or size <= 0 or size % 4:
            raise ValueError('unexpected runtime hook shape')
        relocated = set()
        for rel in obj.section_entries:
            if rel[1] != 4 or rel[7] != section:
                continue
            for position in range(rel[4], rel[4]+rel[5], rel[9]):
                address, info, addend = struct.unpack_from('<QQq', obj.data, position)
                if offset <= address < offset+size:
                    if not 263 <= (info & 0xffffffff) <= 313 or address % 4:
                        raise ValueError('unexpected AArch64 runtime hook relocation')
                    relocated.add(address-offset)
        original = obj.data[source_section[4]+offset:source_section[4]+offset+size]
        emitted = linked.data[destination[4]+offset:destination[4]+offset+size]
        if len(emitted) != size:
            raise ValueError('truncated linked runtime hook')
        verified = 0
        for word in range(0,size,4):
            if word in relocated:
                continue
            if original[word:word+4] != emitted[word:word+4]:
                raise IdentityError('linked runtime hook differs from the archive object')
            verified += 1
        if not verified:
            raise ValueError('runtime hook has no checked instruction words')
        result.update({short+'_va':base+offset, short+'_verified_words':verified})
    return result


def profile_python(args, image, state, directory):
    directory=Path(directory);directory.mkdir(exist_ok=False)
    archive=runtime_archive()
    if archive is None:
        raise ValueError('BOLT instrumentation archive missing; rebuild the LLVM SDK')
    source=Path(image['optimized']);binary=ELF(source)
    if not binary.metadata()['relocations']:
        raise ValueError('native instrumentation requires retained relocations')
    runtime_object=directory/'instr.cpp.o'
    runtime_object.write_bytes(archive_member(archive,'instr.cpp.o'))
    raw=directory/'instrumented.raw.so';instrumented=directory/source.name
    command=[tool('llvm-bolt'),source,'-instrument','-instrumentation-file='+str(directory/'excluded-process-exit.fdata'),
             '-runtime-instrumentation-lib='+str(archive),'-o',raw,'-thread-count=4']
    invocation=run(command,directory/'instrument.log',timeout=getattr(args,'build_timeout',3600))
    normalization=normalize_loads(raw,instrumented)
    hooks=runtime_hooks(runtime_object,instrumented)
    staged=stage_image(image['original'],instrumented,directory/'image')
    # stage_image proves executable sections and their VAs are unchanged.
    profiles=directory/'profiles';profiles.mkdir()
    hook_path=directory/'hooks.json'
    hooks.update(kind='bolt',library=staged['optimized'],sha256=staged['output']['sha256'],
                 directory=str(profiles),log=str(directory/'rois.jsonl'),
                 zero_offset_load_vaddr=next(lo for lo,hi,off,flags,size in ELF(staged['optimized']).loads if off==0))
    write_json(hook_path,hooks)
    runtime=build_runtime(directory/'runtime')
    overrides=dict(state['env']);overrides['TGO_TRAINING_HOOKS']=str(hook_path)
    entry=make_launcher(runtime,runtime/'train',state['command'],[staged],overrides,state['cwd'])
    trials=[]
    expected=state.get('correctness_sha256') if state.get('user_command',state['command'])==state['command'] else None
    env=workload_env();env.update(state['env'])
    for index in range(getattr(args,'train_runs',3)):
        prefix=directory/f'trial-{index:03d}'
        trial_env=dict(env);trial_env['TGO_RESULT_FILE']=str(prefix)+'.result.json'
        code,seconds,timed_out,digest=execute_trial([entry],prefix,trial_env,state['cwd'],args.timeout)
        trials.append({'returncode':code,'seconds':seconds,'timed_out':timed_out,'evidence_prefix':str(prefix)})
        write_json(directory/'progress.json',{'trials':trials})
        if code or timed_out:raise ValueError(f'native BOLT training failed; inspect {prefix}.stderr')
        result=Path(str(prefix)+'.result.json')
        if result.exists():
            digest=hashlib.sha256(json.dumps(read_json(result),sort_keys=True,allow_nan=False).encode()).hexdigest()
            if expected and expected!=digest:raise ValueError('native instrumentation changed workload output')
            expected=digest
    files=sorted(profiles.glob('*.fdata'))
    if not files or any(not p.stat().st_size for p in files):raise ValueError('native ROI emitted no usable profiles')
    merged=directory/'profile.fdata'
    merging=run([tool('merge-fdata'),'-q','-o',merged,*files],directory/'merge.log',timeout=args.timeout)
    record={'schema':'tgo.native-profile.v1','kind':'native-bolt-roi-edges','source':str(source),
        'source_sha256':sha256(source),'profile':str(merged),'sha256':sha256(merged),'hooks':hooks,
        'runtime_archive':str(archive),'runtime_archive_sha256':sha256(archive),
        'instrumentation':invocation,'normalization':normalization,'trials':trials,'merge':merging,
        'roi_profiles':[{'path':str(p),'sha256':sha256(p)} for p in files]}
    write_json(directory/'profile.json',record)
    return record
