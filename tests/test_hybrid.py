from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from tgo.elf import ELF
from tgo.hybrid import compose_profiles,verify_reference
from tgo.profile import qemu_profile
from tgo.util import write_json,read_json,sha256,IdentityError


class HybridCounts(unittest.TestCase):
    def test_independent_sources_keep_units_and_reject_other_versions_and_evidence_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'f.c';source.write_text('int f(void){return 42;}')
            libs=[root/'libpython3.12.so.1.0',root/'libother.so']
            subprocess.run(['cc','-shared','-fPIC',source,'-o',libs[0]],check=True);shutil.copy2(libs[0],libs[1])
            def capture(name):
                out=root/name;out.mkdir()
                write_json(out/'run.json',{'state':'recorded','backend':'qemu','command':[sys.executable,'app.py'],
                    'cwd':str(root),'env':{},'roi':'marker'})
                maps=[];traces=['H\t30\t0\t1\n']
                for index,lib in enumerate(libs):
                    bias=0x70000000+index*0x10000000;lo=next(v[0] for v in ELF(lib).loads if v[3]&1)
                    maps.append(f'M\t{bias:x}\t{lib}\n')
                    traces.append(f'N\t{bias+lo:x}\t{bias+lo+4:x}\t{10+index*10}\nF\t{bias+lo:x}\t{bias+lo+4:x}\t{index+1}\n')
                (out/'capture.1.images').write_text(''.join(maps));(out/'capture.1.trace').write_text(''.join(traces))
                qemu_profile(out);return out
            baseline,library=capture('baseline'),capture('library')
            private=root/'private';private.mkdir();python=private/libs[0].name;shutil.copy2(libs[0],python)
            image={'original':str(libs[0]),'optimized':str(python),'input':ELF(libs[0]).metadata(),'output':ELF(python).metadata()}
            native_dir=root/'native';native_dir.mkdir();native=native_dir/'profile.fdata';native.write_text('1 f 0 1 f 4 0 999\n')
            write_json(native_dir/'profile.json',{'source_sha256':sha256(python)})
            counts={'profile':str(native),'sha256':sha256(native),'trials':[{}],'roi_profiles':[{}]}
            result=compose_profiles(baseline,library,image,counts,root/'composed',['libother.so'])
            py=next(r for r in result['images'] if r['path']==str(python))
            other=next(r for r in result['images'] if r['path']==str(libs[1]))
            self.assertEqual(py['sample_percent'],100/3)
            self.assertNotIn('preaggregate',py);self.assertEqual(py['native_fdata'],str(native))
            self.assertEqual(other['profile_backend'],'qemu');self.assertNotIn('native_fdata',other)
            profile=read_json(library/'profile.json');profile['images'][0]['sha256']='0'*64
            # The first profile row is libother (higher dynamic count).
            write_json(library/'profile.json',profile)
            with self.assertRaisesRegex(ValueError,'required recipe inputs'):
                compose_profiles(baseline,library,image,counts,root/'wrong-version',['libother.so'])
            with self.assertRaises(IdentityError):verify_reference(result['library_capture_ref'])


if __name__=='__main__':unittest.main()
