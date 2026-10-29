from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tgo.elf import ELF
from tgo.perf import perf_profile
from tgo.util import read_json, write_json


class PerfCoverage(unittest.TestCase):
    def normalize(self, root, report, loader_paths=()):
        write_json(root/'run.json', {'backend':'spe'})
        (root/'perf.data').write_bytes(b'raw fixture')
        (root/'capture.1.images').write_text(''.join(f'M\t0\t{p}\n' for p in loader_paths))
        response = subprocess.CompletedProcess([], 0, stdout=report, stderr='')
        with patch('tgo.perf.subprocess.check_output', return_value=''), patch('tgo.perf.subprocess.run', return_value=response):
            return perf_profile(root, 'unused-perf')

    def test_dominant_deleted_mapping_is_preserved_as_evidence_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); binary=ELF(sys.executable).metadata()['path']
            with self.assertRaisesRegex(ValueError, 'most samples'):
                self.normalize(root, f'93.35%\t/ (deleted)\n6.65%\t{binary}\n')
            quality=read_json(root/'profile.json')['quality']
            self.assertAlmostEqual(quality['unresolved_sample_percent'],93.35)
            self.assertAlmostEqual(quality['resolved_sample_percent'],6.65)
            self.assertEqual(quality['unresolved_dsos'][0]['name'],'/ (deleted)')

    def test_minor_unknown_code_is_reported_and_symlink_aliases_do_not_duplicate_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); binary=Path(sys.executable).resolve(); alias=root/'interpreter';alias.symlink_to(binary)
            result=self.normalize(root, f'60.00%\t{binary}\n39.00%\t{alias}\n1.00%\t[unknown]\n', [binary,alias])
            self.assertEqual(len(result['images']),1)
            self.assertEqual(result['images'][0]['sample_percent'],99.0)
            self.assertEqual(result['quality']['unresolved_sample_percent'],1.0)
            self.assertEqual(result['quality']['unresolved_dsos'],[{'name':'[unknown]','sample_percent':1.0}])


if __name__=='__main__':
    unittest.main()
