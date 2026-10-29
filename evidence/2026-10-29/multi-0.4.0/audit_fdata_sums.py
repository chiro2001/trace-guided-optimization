import collections
import json
from pathlib import Path
import statistics
import sys

root = Path.cwd()
sys.path.insert(0, str(root))
from tgo.capture_set import verified_sources
from tgo.util import sha256


def counts(path):
    rows = collections.Counter()
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == 'no_lbr':
            continue
        if len(fields) == 4:
            rows[tuple(fields[:3])+('count',)] += int(fields[3])
        elif len(fields) == 8:
            rows[tuple(fields[:6])+('mispred',)] += int(fields[6])
            rows[tuple(fields[:6])+('count',)] += int(fields[7])
        elif fields:
            raise ValueError(f'unexpected branch fdata record: {line[:100]}')
    if not rows:
        raise ValueError(f'empty fdata: {path}')
    return rows


def summarize(name):
    directory = root/'runs'/name
    data = json.loads((directory/'tune.json').read_text())
    result = {key: data.get(key) for key in ('state','training_runs_completed','selected','entry','optimized_images','error')}
    result.update(run=str(directory), tune_sha256=sha256(directory/'tune.json'))
    if data['state'] != 'complete':
        return result
    profile_path = Path(data['capture'])/'profile.json'
    profile = json.loads(profile_path.read_text())
    result['profile_sha256'] = sha256(profile_path)
    if profile.get('sources'):
        verified_sources(profile)
        result['capture_set'] = {key: profile[key] for key in ('schema','backend','sources','quality')}
    candidates = []
    for candidate in data['candidates']:
        item = {'name': candidate['name']}
        if candidate.get('validation'):
            validation = candidate['validation']
            item.update({key: validation[key] for key in ('equivalent','completed_pairs','median_reduction_percent','min_reduction_percent','max_reduction_percent')})
        item['error'] = candidate.get('error')
        build_path = Path(candidate.get('directory', directory/candidate['name']))/'build.json'
        if build_path.exists():
            built = json.loads(build_path.read_text())
            item['images'] = []
            for image in built.get('images', []):
                aggregation = image['aggregation']
                audit = {'original':image['original'],'input_sha256':image['input']['sha256'],'output_sha256':image['output']['sha256']}
                if aggregation.get('strategy') == 'independent-ELF-normalization-and-count-merge':
                    expected = collections.Counter()
                    inputs = [v for v in aggregation['inputs'] if v['used']]
                    for source in inputs:
                        if sha256(source['path']) != source['sha256']: raise ValueError('fdata changed')
                        expected.update(counts(source['path']))
                    merged = Path(aggregation['merge']['command'][aggregation['merge']['command'].index('-o')+1])
                    actual = counts(merged)
                    if actual != expected: raise ValueError(f'fdata counts differ: {merged}')
                    audit.update(fdata_sum_verified=True, input_captures=len(inputs), count_records=sum(1 for k in actual if k[-1]=='count'),
                        total_count=sum(v for k,v in actual.items() if k[-1]=='count'),fdata_sha256=sha256(merged),
                        input_fdata=[{k:v[k] for k in ('path','sha256')} for v in inputs])
                item['images'].append(audit)
        candidates.append(item)
    result['candidates'] = candidates
    if data.get('confirmation'):
        validation = data['confirmation']
        result['confirmation'] = {key: validation[key] for key in ('equivalent','completed_pairs','median_reduction_percent','min_reduction_percent','max_reduction_percent')}
        pmu_changes = collections.defaultdict(list)
        all_valid = True
        for pair in validation['pairs']:
            for arm in ('baseline','candidate'):
                for row in pair[arm].get('pmu',[]):
                    if row['open_errors'] or not row['no_multiplex'] or row['time_enabled'] != row['time_running']: all_valid = False
            for event in ('cycles','instructions','frontend_stall_cycles','l1i_read_misses'):
                values = {arm:[row['events'][event] for row in pair[arm].get('pmu',[]) if event in row['events']] for arm in ('baseline','candidate')}
                if all(values.values()):
                    pmu_changes[event].append(100*(1-statistics.median(values['candidate'])/statistics.median(values['baseline'])))
        result['pmu'] = {'all_enabled_equal_running_no_open_errors':all_valid,
            'median_reduction_percent':{event:statistics.median(values) for event,values in pmu_changes.items()}}
    return result

name = sys.argv[1]
report = summarize(name)
if report['state'] == 'complete':
    output = root/'runs'/name/'multi-summary.json'
    output.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
