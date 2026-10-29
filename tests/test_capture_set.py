import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tgo.capture_set import merge_captures, observations, fingerprint, verified_sources, SCHEMA
from tgo.elf import ELF
from tgo.optimize import aggregate_image, optimize
from tgo.profile import qemu_profile
from tgo.tune import tune
from tgo.util import IdentityError, read_json, write_json, sha256, tool, tool_env


class CaptureSets(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'lib.c'
        self.source.write_text('__attribute__((noinline)) int f(int x){return x+7;}\n')
        self.lib = self.root / 'libfixture.so'
        subprocess.run(['cc', '-shared', '-fPIC', '-O1', '-Wl,--emit-relocs', self.source, '-o', self.lib], check=True)
        symbols = subprocess.check_output(['nm', '-D', '--defined-only', self.lib], text=True)
        self.pc = int(next(line.split()[0] for line in symbols.splitlines() if line.split()[-1] == 'f'), 16)

    def capture(self, name, count=10, bias=0x70000000):
        root = self.root / name; root.mkdir(parents=True)
        write_json(root / 'run.json', {'state': 'recorded', 'backend': 'qemu', 'command': [sys.executable, '-c', 'print(7)'],
            'cwd': str(self.root), 'env': {}, 'roi': 'marker', 'started_unix': count, 'finished_unix': count+1})
        (root / 'capture.1.images').write_text(f'M\t{bias:x}\t{self.lib}\n')
        pc = bias + self.pc
        (root / 'capture.1.trace').write_text(f'H\t{count}\t0\t1\nN\t{pc:x}\t{pc+4:x}\t{count}\n'
            f'B\t0\t{pc:x}\t{count}\nF\t{pc:x}\t{pc+4:x}\t{count}\n')
        qemu_profile(root)
        return root

    def perf_capture(self, name, count=10, weight=50):
        root = self.capture(name, count)
        state = read_json(root/'run.json')
        state.update(backend='perf', collector_command=['perf', 'record', '-e', 'cycles:u', '-F', '999', '--', 'app', '-c', 'not-a-period'])
        write_json(root/'run.json', state)
        raw = root/'perf.data'; raw.write_bytes(bytes([count]))
        row = ELF(self.lib).metadata(); row.update(sample_percent=weight, perf_data=str(raw))
        write_json(root/'profile.json', {'backend': 'perf', 'images': [row], 'raw_data': {'path': str(raw), 'sha256': sha256(raw)}})
        return root

    def test_aslr_is_normalized_per_source_and_nested_sets_flatten_without_double_counting(self):
        a, b, c = self.capture('a', 10), self.capture('b', 20, 0x80000000), self.capture('c', 30, 0x90000000)
        merged = merge_captures([a, b], self.root/'ab')
        self.assertEqual(merged['images'][0]['dynamic_instructions'], 30)
        self.assertEqual(merged['instructions'], 30)
        self.assertEqual(len(merged['images'][0]['observations']), 2)
        self.assertNotIn('preaggregate', merged['images'][0])
        rows = observations(merged, merged['images'][0])
        for row, count in zip(rows, (10, 20)):
            self.assertIn(f'F {self.pc:x} {self.pc+4:x} {count}', Path(row['preaggregate']).read_text())
        nested = merge_captures([self.root/'ab', c], self.root/'abc')
        self.assertEqual(nested['images'][0]['dynamic_instructions'], 60)
        self.assertEqual([r['run'] for r in nested['sources']], list(map(str, (a, b, c))))
        for sources in ([self.root/'ab', a], [a, a]):
            with self.assertRaisesRegex(ValueError, 'duplicate capture'):
                merge_captures(sources, self.root/'duplicate')
        copied = self.root/'copied'; shutil.copytree(a, copied)
        with self.assertRaisesRegex(ValueError, 'duplicate capture'):
            merge_captures([a, copied], self.root/'copied-duplicate')

    def test_sample_share_missing_observations_are_zero_and_only_positive_sources_are_used(self):
        a, b = self.perf_capture('a', 10, 80), self.perf_capture('b', 20, 0)
        merged = merge_captures([a, b], self.root/'merged')
        self.assertEqual(merged['images'][0]['sample_percent'], 40)
        self.assertEqual(merged['images'][0]['observations'], [{'source': 0}])
        self.assertNotIn('perf_data', merged['images'][0])
        profile = read_json(b/'profile.json'); profile['images'] = []; write_json(b/'profile.json', profile)
        absent = merge_captures([a, b], self.root/'absent')
        self.assertEqual(absent['images'][0]['sample_percent'], 40)

    def test_sampler_and_elf_contracts_remain_strict_when_workload_variation_is_allowed(self):
        a, b = self.perf_capture('a'), self.perf_capture('b', 20)
        baseline = read_json(b/'run.json')
        # New explicit metadata and legacy collector argv must compare equally.
        sampling = fingerprint(baseline)
        self.assertEqual(sampling['frequency'], 999)
        state = {**baseline, 'sampling': sampling}; write_json(b/'run.json', state)
        merge_captures([a, b], self.root/'compatible')
        for field, value in [('event', 'instructions:u'), ('frequency', 100), ('period', 4096), ('branch_stack', True), ('backend', 'spe')]:
            with self.subTest(field=field):
                state['sampling'] = {**sampling, field: value}; write_json(b/'run.json', state)
                with self.assertRaisesRegex(ValueError, 'incompatible'):
                    merge_captures([a, b], self.root/'bad-sampler', allow_variation=True)
        write_json(b/'run.json', baseline)
        profile = read_json(b/'profile.json'); profile['images'][0]['sha256'] = 'f'*64; write_json(b/'profile.json', profile)
        with self.assertRaisesRegex(ValueError, 'ELF identity differs'):
            merge_captures([a, b], self.root/'bad-elf', allow_variation=True)

    def test_roi_bounds_and_hooks_are_compared_and_deliberate_variation_is_recorded(self):
        a, b = self.perf_capture('a'), self.perf_capture('b', 20)
        for root, bounds in ((a, '1000-2000'), (b, '1000-3000')):
            state = read_json(root/'run.json'); state['roi'] = 'window'
            state['collector_command'].insert(2, '--delay='+bounds); write_json(root/'run.json', state)
        with self.assertRaisesRegex(ValueError, 'workload/ROI differs'):
            merge_captures([a, b], self.root/'bad-window')
        merged = merge_captures([a, b], self.root/'variants', allow_variation=True)
        self.assertEqual(merged['quality']['workload_variations'], [{'source': 1, 'different_fields': ['roi_window']}])
        for root in (a, b):
            state = read_json(root/'run.json'); user = state['command']
            state.update(roi='marker', user_command=user, command=[user[0], str(root/'python_entry.py'), '--hook', 'app:decode', '--', *user[1:]])
            write_json(root/'run.json', state)
        merge_captures([a, b], self.root/'hooks')
        state['command'][3] = 'app:prefill'; write_json(b/'run.json', state)
        with self.assertRaisesRegex(ValueError, 'workload/ROI differs'):
            merge_captures([a, b], self.root/'bad-hook')

    def test_source_evidence_drift_aborts_optimization_before_any_rewriter(self):
        for name in ('run.json', 'profile.json', 'capture.1.trace', 'capture.1.images', 'preaggregate', 'perf.data'):
            with self.subTest(name=name):
                source = self.perf_capture(name.replace('.', '-')+'-source') if name == 'perf.data' else self.capture(name.replace('.', '-')+'-source')
                output = self.root/(source.name+'-set'); merged = merge_captures([source], output)
                path = Path(observations(merged, merged['images'][0])[0]['preaggregate']) if name == 'preaggregate' else source/name
                with path.open('ab') as stream: stream.write(b'changed')
                args = argparse.Namespace(run=str(output), output=str(self.root/(source.name+'-out')), image=[], top=1, allow_stripped=False, bolt_mode='auto')
                with patch('tgo.optimize.rewrite_image') as rewrite:
                    with self.assertRaises(IdentityError): optimize(args)
                    rewrite.assert_not_called()
                self.assertEqual(read_json(Path(args.output)/'build.json')['state'], 'failed')
                self.assertFalse((Path(args.output)/'run').exists())

    def test_drift_during_an_object_failure_is_fatal_after_the_rewrite(self):
        source = self.capture('source'); merged = self.root/'merged'; merge_captures([source], merged)
        args = argparse.Namespace(run=str(merged), output=str(self.root/'out'), image=[], top=1, allow_stripped=False, bolt_mode='auto')
        def failing_rewrite(*args):
            with (source/'capture.1.trace').open('ab') as stream: stream.write(b'changed')
            raise RuntimeError('rewriter failed')
        with patch('tgo.optimize.image_policy', return_value={}), patch('tgo.optimize.rewrite_image', side_effect=failing_rewrite):
            with self.assertRaises(IdentityError): optimize(args)
        self.assertFalse((self.root/'out/run').exists())

    def test_real_perf2bolt_and_merge_fdata_add_counts_after_independent_mapping(self):
        p2b, merger = tool('perf2bolt', required=False), tool('merge-fdata', required=False)
        if not p2b or not merger:
            self.skipTest('real perf2bolt and merge-fdata required')
        a, b = self.capture('a', 10), self.capture('b', 20, 0x80000000)
        profile = merge_captures([a, b], self.root/'merged')
        directory = self.root/'aggregation'; directory.mkdir()
        args = argparse.Namespace(timeout=30, branch_stack=False)
        fdata, record = aggregate_image(args, profile['images'][0], {}, profile, directory,
            {'perf2bolt': p2b, 'mode': 'standard', 'allow_stripped': False}, tool_env())
        def counts(path):
            rows = {}
            for line in Path(path).read_text().splitlines():
                parts = line.split()
                if len(parts) >= 8:
                    rows[tuple(parts[:-2])] = tuple(map(int, parts[-2:]))
            self.assertTrue(rows, 'expected real branch/count records')
            return rows
        sources = [counts(item['path']) for item in record['inputs']]
        expected = {key: tuple(sum(rows.get(key, (0, 0))[i] for rows in sources) for i in range(2)) for key in set().union(*sources)}
        self.assertEqual(counts(fdata), expected)
        self.assertTrue(all(item['used'] for item in record['inputs']))
        self.assertEqual(record['merge_tool_sha256'], sha256(merger))
        # A library present in only one run must also yield a usable profile.
        one = merge_captures([a], self.root/'one'); directory = self.root/'single'; directory.mkdir()
        single, _ = aggregate_image(args, one['images'][0], {}, one, directory,
            {'perf2bolt': p2b, 'mode': 'standard', 'allow_stripped': False}, tool_env())
        self.assertEqual(counts(single), sources[0])

    def test_fresh_tune_locks_backend_merges_three_runs_and_leaves_a_working_baseline(self):
        calls = []
        def record(args):
            calls.append((args.backend, args.event))
            self.capture(str(Path(args.output).relative_to(self.root)), 10*len(calls), 0x70000000*len(calls))
        args = argparse.Namespace(run=None, output=str(self.root/'tuned'), strategy='bolt', train_runs=3, backend='auto', event=None,
            image=[], bolt_mode='auto', image_search='joint', layout=['ext-tsp'], bolt_arg=[], entry=None, entry_mode='command',
            confirm_candidates=2, minimum_gain=0.5)
        with patch('tgo.tune.record', side_effect=record), patch('tgo.tune.optimize', side_effect=RuntimeError('tools unavailable in fixture')):
            result = tune(args)
        self.assertEqual(calls, [('auto', None), ('qemu', None), ('qemu', None)])
        self.assertEqual(result['training_runs_completed'], 3)
        self.assertEqual(read_json(self.root/'tuned/capture/profile.json')['schema'], SCHEMA)
        self.assertEqual(subprocess.check_output([result['entry']], text=True), '7\n')

    def test_go_discovery_stops_sampling_early_and_persists_the_adapter_result(self):
        def record(args):
            self.capture(str(Path(args.output).relative_to(self.root)))
        args = argparse.Namespace(run=None, output=str(self.root/'tuned'), strategy='auto', train_runs=3)
        adapter = {'state': 'complete', 'strategy': 'go-compiler-pgo', 'selected': 'baseline', 'training_runs_completed': 3}
        with patch('tgo.tune.record', side_effect=record) as recording, patch('tgo.tune.discover_go', return_value=adapter):
            result = tune(args)
        self.assertEqual(recording.call_count, 1)
        self.assertEqual(result, adapter)
        self.assertEqual(read_json(self.root/'tuned/tune.json'), adapter)


if __name__ == '__main__':
    unittest.main()
