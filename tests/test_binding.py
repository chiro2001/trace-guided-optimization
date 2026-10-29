from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from tgo.artifact import executable_sections
from tgo.binding import bind_now
from tgo.util import sha256


class EagerBinding(unittest.TestCase):
    def test_only_a_single_nonexecuting_dynamic_entry_can_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'f.c';source.write_text('int f(void){return 42;}')
            binary=root/'original.so';subprocess.run(['cc','-shared','-fPIC',source,'-o',binary],check=True)
            # Dynamic table layout is ELF64 on both hosts. This is a data-only
            # fixture, never executed with the modified architecture field.
            raw=bytearray(binary.read_bytes());struct.pack_into('<H',raw,18,183);binary.write_bytes(raw)
            digest=sha256(binary);code=executable_sections(binary)
            target=root/'bound.so';record=bind_now(binary,target)
            self.assertEqual(sha256(binary),digest)
            self.assertEqual(executable_sections(target),code)
            offset=record['dynamic_entry_offset'];output=target.read_bytes()
            self.assertEqual(output[:offset],bytes(raw[:offset]))
            self.assertEqual(output[offset+16:],bytes(raw[offset+16:]))
            self.assertTrue(record['executable_instructions_unchanged'])
            with self.assertRaises(FileExistsError):bind_now(binary,target)


if __name__=='__main__':unittest.main()
