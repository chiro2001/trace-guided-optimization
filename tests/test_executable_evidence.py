from pathlib import Path
import subprocess
import tempfile
import unittest
from tgo.elf import ELF
from tgo.tune import compose_bundle
from tgo.util import write_json
from tgo.validate import validate

class ExecutableEvidence(unittest.TestCase):
    def test_dynamic_executable_must_be_the_image_actually_loaded_even_when_output_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'f.c';source.write_text('#include <stdio.h>\nint main(void){puts("same-output");return 0;}')
            original=root/'original';candidate=root/'candidate'
            for output in [original,candidate]:subprocess.run(['cc',source,'-o',output],check=True)
            image={'original':str(original),'optimized':str(candidate),'input':ELF(original).metadata(),'output':ELF(candidate).metadata()}
            for disabled in [False,True]:
                capture=root/('capture-disabled' if disabled else 'capture');capture.mkdir()
                write_json(capture/'run.json',{'command':[str(original)],'cwd':str(root),
                    'env':{'TGO_DISABLE':'1'} if disabled else {},'state':'recorded'})
                bundle=root/('disabled' if disabled else 'enabled')
                compose_bundle(bundle,{'source_run':str(capture)},[image])
                result=validate(bundle,pairs=1,timeout=10)
                self.assertEqual(result['equivalent'],not disabled)
                observed=result['pairs'][0]['candidate']['loaded_optimized']
                self.assertEqual(observed,[] if disabled else [str(candidate)])

if __name__=='__main__':unittest.main()
