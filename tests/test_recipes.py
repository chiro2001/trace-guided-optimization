import argparse
import io
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from tgo.capabilities import inspect_image
from tgo.cpython import source_archive
from tgo.elf import ELF
from tgo.recipes import resolve_recipe,recovered_policy
from tgo.optimize import verify_identity,select_images
from tgo.util import sha256,IdentityError

class StructuralQualification(unittest.TestCase):
    def compile(self,root,name,body):
        source=root/(name+'.c');target=root/(name+'.so')
        source.write_text(body)
        subprocess.run(['cc','-shared','-fPIC',source,'-o',target],check=True)
        return target

    def test_unseen_different_files_qualify_without_digest_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            targets=[self.compile(root,'new-version-a','int f(int x){return x+1;}'),
                     self.compile(root,'unrelated-name-b','int f(int x){return x*3+2;}')]
            rows=[{**ELF(p).metadata(),'sample_percent':50} for p in targets]
            self.assertNotEqual(rows[0]['sha256'],rows[1]['sha256'])
            args=argparse.Namespace(recipe='auto',top=2,image=[],bolt_mode='auto',allow_stripped=False)
            with patch('tgo.util.sha256',side_effect=AssertionError('qualification must not hash')):
                args._recipe_plan=resolve_recipe(args,{}, {'images':rows})
            self.assertFalse(args._recipe_plan['known_input_allowlist'])
            self.assertTrue(all(o['offline']['candidate'] for o in args._recipe_plan['observations']))
            selected,_=select_images(args,{'command':[sys.executable]}, {'images':rows})
            self.assertEqual({r['path'] for r in selected},{str(p) for p in targets})
            self.assertFalse(any(recovered_policy(args,row) for row in rows))
            args.recipe='eager-v12'
            self.assertTrue(resolve_recipe(args,{}, {'images':rows})['compatibility_alias'])

    def test_runtime_recognition_uses_go_metadata_not_file_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            target=self.compile(root,'python3.99',
                '__attribute__((section(".gopclntab"))) const char meta[16]={1}; int f(void){return 5;}')
            observed=inspect_image(target)
            self.assertEqual(observed['runtime'],'go')
            self.assertIn('go_pc_stack_gc_metadata_requires_go_adapter',observed['offline']['requirements'])

    def test_renamed_cpython_is_still_identified(self):
        library=Path(sysconfig.get_config_var('LIBDIR'))/sysconfig.get_config_var('LDLIBRARY')
        if not library.is_file():self.skipTest('interpreter has no shared core')
        with tempfile.TemporaryDirectory() as folder:
            renamed=Path(folder)/'future-runtime-with-no-python-name.blob'
            shutil.copy2(library,renamed)
            with patch('tgo.util.sha256',side_effect=AssertionError('must not hash')):
                self.assertEqual(inspect_image(renamed)['runtime'],'cpython')

    def test_profile_identity_still_rejects_different_live_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=self.compile(root,'version','int f(void){return 1;}')
            old=sha256(target)
            self.compile(root,'version','int f(void){return 2;}')
            self.assertTrue(inspect_image(target)['offline']['candidate'])
            with self.assertRaises(IdentityError):verify_identity(target,old)

    def test_future_explicit_cpython_source_is_not_locked_to_a_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'any-name.tgz'
            with tarfile.open(archive,'w:gz') as f:
                content=b'#define PY_VERSION "9.99.7"\n'
                member=tarfile.TarInfo('Python-9.99.7/Include/patchlevel.h');member.size=len(content)
                f.addfile(member,io.BytesIO(content))
            args=argparse.Namespace(python_source=str(archive))
            path,record=source_archive('9.99.7',args)
            self.assertEqual(path,archive)
            self.assertEqual(record['version'],'9.99.7')
            with self.assertRaisesRegex(ValueError,'differs from interpreter'):source_archive('9.99.8',args)

    def test_malformed_elf_is_a_diagnostic_not_an_unhandled_struct_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'short';path.write_bytes(b'\x7fELF\x02\x01'+bytes(12))
            with self.assertRaises(ValueError):inspect_image(path)
            target=self.compile(Path(folder),'real','int f(void){return 0;}')
            raw=bytearray(target.read_bytes());struct.pack_into('<Q',raw,40,len(raw)+100)
            target.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'outside ELF'):inspect_image(target)

if __name__=='__main__':unittest.main()
