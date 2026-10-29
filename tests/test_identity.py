from pathlib import Path
import hashlib
import subprocess
import tempfile
import unittest
from tgo.runtime import NATIVE


class NativeContentIdentity(unittest.TestCase):
    def test_sha256_matches_standard_implementation_across_padding_and_read_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/"hash.c";binary=root/"hash"
            source.write_text('#include <stdio.h>\n#include "tgo_sha256.h"\n'
                'int main(int argc,char **argv){unsigned char h[32];if(argc!=2||tgo_sha256_file(argv[1],h))return 1;'
                'for(int i=0;i<32;i++)printf("%02x",h[i]);puts("");return 0;}\n')
            subprocess.run(["cc","-O2","-I",NATIVE/"include",source,"-o",binary],check=True)
            for data in (b"",b"abc",*[b"a"*n for n in (55,56,63,64,65)],bytes(range(256))*4097):
                path=root/"payload";path.write_bytes(data)
                self.assertEqual(subprocess.check_output([binary,path],text=True).strip(),hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    unittest.main()
