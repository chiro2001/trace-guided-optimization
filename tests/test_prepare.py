from pathlib import Path
import subprocess
import tempfile
import unittest
from tgo.elf import ELF
from tgo.prepare import prepare
from tgo.capabilities import inspect_image

class PreparedELF(unittest.TestCase):
    def test_debugless_input_keeps_file_provenance_code_and_bss_even_with_manifest_like_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'fixture.c';source.write_text('int data[1024]; int main(void){return data[3];}')
            original=root/'input.json';subprocess.run(['cc','-g','-Wl,--emit-relocs',source,'-o',original],check=True)
            result=prepare(original,root/'prepared',strip_debug=True)
            image=Path(result['image']);self.assertEqual(image.name,'input.json')
            self.assertEqual(image.parent.name,'image');self.assertTrue((root/'prepared/input.json').is_file())
            self.assertTrue(result['all_allocated_sections_unchanged']);self.assertGreater(result['symbols']['file_symbols'],0)
            self.assertNotIn('.debug_info',ELF(image).sections)
            subprocess.run([image],check=True)
            with self.assertRaises(FileExistsError):prepare(original,root/'prepared',strip_debug=True)

    def test_code_relocation_detection_uses_linked_section_flags_instead_of_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'fixture.c';source.write_text('int f(void){return 4;}')
            original=root/'original.so';subprocess.run(['cc','-shared','-fPIC','-Wl,--emit-relocs',source,'-o',original],check=True)
            renamed=root/'renamed.so';subprocess.run(['objcopy','--rename-section','.text=.future_code',
                '--rename-section','.rela.text=.future_relocations',original,renamed],check=True)
            self.assertTrue(inspect_image(renamed)['retained_code_relocations'])
            self.assertTrue(ELF(renamed).metadata()['relocations'])

if __name__=='__main__':unittest.main()
