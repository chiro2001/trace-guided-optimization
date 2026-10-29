import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tgo.optimize import aggregate_image
from tgo.util import IdentityError,sha256,write_json


class NativeEdges(unittest.TestCase):
    def test_native_counts_are_not_mixed_with_sampling_units_and_must_match_the_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);profile=root/'native.fdata';profile.write_text('1 f 0 1 f 4 0 1234\n')
            manifest=root/'native.json';write_json(manifest,{'source_sha256':'a'*64})
            row={'path':'/input/libpython.so','sha256':'a'*64,'perf_data':'unused-sampling.data',
                 'native_fdata':str(profile),'native_fdata_sha256':sha256(profile),
                 'native_profile':str(manifest),'native_profile_sha256':sha256(manifest)}
            out=root/'out';out.mkdir()
            with patch('tgo.optimize.aggregate_observation') as converter:
                generated,record=aggregate_image(argparse.Namespace(),row,{}, {'backend':'spe'},out,{}, {})
                converter.assert_not_called()
            self.assertEqual(generated.read_text(),profile.read_text())
            self.assertEqual(record['strategy'],'native-bolt-roi-edges')
            row['sha256']='b'*64
            with self.assertRaises(IdentityError):aggregate_image(argparse.Namespace(),row,{}, {'backend':'spe'},out,{}, {})
            row['sha256']='a'*64;profile.write_text('changed')
            with self.assertRaises(IdentityError):aggregate_image(argparse.Namespace(),row,{}, {'backend':'spe'},out,{}, {})


if __name__=='__main__':unittest.main()
