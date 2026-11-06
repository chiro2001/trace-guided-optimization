import os
import io
from pathlib import Path
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tgo.collect import doctor
from tgo.cpython import source_archive
from tgo.instrument import runtime_archive
from tgo.util import IdentityError,sha256,write_json


class PackagedCompilerInputs(unittest.TestCase):
    def test_new_sdk_prefix_supplies_nonexecutables_to_recording_and_frozen_jobs(self):
        from tgo.jobs import frozen_tool_environment
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sdk=root/'future-sdk';header=sdk/'include/qemu-plugin.h';runtime=sdk/'lib/libbolt_rt_instr.a'
            for path in [header,runtime]:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'new SDK input')
            with patch.dict(os.environ,{'TGO_INSTALL_PREFIX':str(sdk)},clear=True),patch('tgo.collect.tool',return_value=None):
                self.assertEqual(runtime_archive(),runtime)
                self.assertEqual(doctor()['sdk_assets']['qemu_header'],str(header))
                environment,_=frozen_tool_environment(project=root/'unrelated-project')
                self.assertEqual(environment['TGO_QEMU_HEADER'],str(header))
                self.assertEqual(environment['TGO_BOLT_RUNTIME'],str(runtime))

    def test_offline_assets_are_visible_and_content_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'usr/share/tgo/sources/Python-3.12.13.tgz'
            runtime=root/'usr/lib/tgo-bolt/libbolt_rt_instr.a'
            for p in (source,runtime):p.parent.mkdir(parents=True)
            runtime.write_bytes(b'locked input')
            with tarfile.open(source,'w:gz') as archive:
                content=b'#define PY_VERSION "3.12.13"\n'
                member=tarfile.TarInfo('Python-3.12.13/Include/patchlevel.h');member.size=len(content)
                archive.addfile(member,io.BytesIO(content))
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
