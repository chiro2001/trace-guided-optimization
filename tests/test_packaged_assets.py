import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tgo.collect import doctor
from tgo.cpython import source_archive
from tgo.instrument import runtime_archive
from tgo.util import IdentityError,sha256,write_json


class PackagedCompilerInputs(unittest.TestCase):
    def test_offline_assets_are_visible_and_content_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'usr/share/tgo/sources/Python-3.12.13.tgz'
            runtime=root/'usr/lib/tgo-bolt/libbolt_rt_instr.a'
            for p in (source,runtime):p.parent.mkdir(parents=True);p.write_bytes(b'locked input')
            write_json(root/'dependencies.lock.json',{'python-gzip':{
                'version':'3.12.13','url':'https://example.test/'+source.name,'sha256':sha256(source)}})
            write_json(root/'package-manifest.json',{'compiler_inputs':{
                'bolt_runtime':{'sha256':sha256(runtime)}}})
            with patch.dict(os.environ,{'TGO_PACKAGED':'1','TGO_APPDIR':str(root)}),patch('tgo.collect.tool',return_value=None):
                self.assertEqual(source_archive('3.12.13',SimpleNamespace())[0],source)
                self.assertEqual(runtime_archive(),runtime)
                found=doctor()['packaged_compiler_inputs']
                self.assertTrue(found['source_matches_lock']);self.assertTrue(found['bolt_runtime_matches_manifest'])
                source.write_bytes(b'changed source');runtime.write_bytes(b'changed runtime')
                with self.assertRaises(IdentityError):source_archive('3.12.13',SimpleNamespace())
                found=doctor()['packaged_compiler_inputs']
                self.assertFalse(found['source_matches_lock']);self.assertFalse(found['bolt_runtime_matches_manifest'])
                del os.environ['TGO_APPDIR']
                self.assertFalse(doctor()['packaged_compiler_inputs']['appdir_exported'])


if __name__=='__main__':unittest.main()
