import struct
from types import SimpleNamespace
import unittest
from tgo.live.correspondence import functions

def image(addresses,targets,slots=None):
    slots=slots or targets
    data=struct.pack('<'+'Q'*len(slots),*slots)
    relocs=b''.join(struct.pack('<QQq',0x4000+8*i,1027,value) for i,value in enumerate(targets))
    sections=[{'name':'.data','type':1,'flags':3,'addr':0x4000,'size':len(data),'entsize':0,'blob':data},
              {'name':'.rela.dyn','type':4,'flags':2,'addr':0x5000,'size':len(relocs),'entsize':24,'blob':relocs}]
    return SimpleNamespace(sections=sections,blob=lambda section:section['blob'],symbols=[
        {'name':'same_local_name','type':2,'size':16,'value':address,'shndx':1} for address in addresses])

class LiveCorrespondence(unittest.TestCase):
    def test_duplicate_locals_require_matching_relocation_slot_and_raw_value(self):
        old=image([0x1000,0x1100],[0x1000,0x1100]);new=image([0x8000,0x8100],[0x8100,0x8000])
        mapped,rejected=functions(old,new,[{'addr':0x8000,'size':0x1000,'flags':6}])
        self.assertEqual(rejected,[])
        self.assertEqual({row['old_vaddr']:row['new_vaddr'] for row in mapped},{0x1000:0x8100,0x1100:0x8000})
        self.assertTrue(all(row['relocation_witnesses'][0]['data_words_verified'] for row in mapped))
        broken=image([0x8000,0x8100],[0x8100,0x8000],[0x8000,0x8100])
        with self.assertRaisesRegex(ValueError,'addend不一致'):functions(old,broken,[{'addr':0x8000,'size':0x1000,'flags':6}])

    def test_missing_or_conflicting_witness_cannot_guess_by_symbol_order(self):
        parts=[{'addr':0x8000,'size':0x1000,'flags':6}]
        old=image([0x1000,0x1100],[]);new=image([0x8000,0x8100],[])
        mapped,rejected=functions(old,new,parts)
        self.assertEqual(mapped,[]);self.assertEqual(len(rejected),2)
        old=image([0x1000],[0x1000,0x1000]);new=image([0x8000,0x8100],[0x8000,0x8100])
        with self.assertRaisesRegex(ValueError,'互相冲突'):functions(old,new,parts)

    def test_one_emitted_duplicate_cannot_redirect_two_different_original_functions(self):
        parts=[{'addr':0x8000,'size':0x1000,'flags':6}]
        old=image([0x1000,0x1100],[]);new=image([0x8000],[])
        mapped,rejected=functions(old,new,parts)
        self.assertEqual(mapped,[]);self.assertEqual(len(rejected),2)
        old=image([0x1000,0x1100],[0x1100]);new=image([0x8000],[0x8000])
        mapped,rejected=functions(old,new,parts)
        self.assertEqual([(r['old_vaddr'],r['new_vaddr']) for r in mapped],[(0x1100,0x8000)])
        self.assertEqual([r['old_vaddr'] for r in rejected],[0x1000])

    def test_constructor_arrays_are_typed_pointer_slots(self):
        old=image([0x1000,0x1100],[0x1000,0x1100]);new=image([0x8000,0x8100],[0x8100,0x8000])
        for elf in [old,new]:elf.sections[0].update(name='.init_array',type=14)
        mapped,rejected=functions(old,new,[{'addr':0x8000,'size':0x1000,'flags':6}])
        self.assertEqual(rejected,[]);self.assertEqual(len(mapped),2)

if __name__=='__main__':unittest.main()
