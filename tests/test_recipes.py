import argparse
from pathlib import Path
import unittest
from tgo.recipes import EAGER_V12,resolve_recipe,recovered_policy
from tgo.util import IdentityError


class RecipeIdentity(unittest.TestCase):
    def fixture(self):return {'images':[{'path':'/original/'+name,'sha256':digest} for name,digest in EAGER_V12.items()]}

    def test_full_policy_needs_every_known_input_and_does_not_expand_to_other_versions(self):
        args=argparse.Namespace(recipe='auto');profile=self.fixture()
        args._recipe_plan=resolve_recipe(args,{},profile)
        self.assertEqual(len(args._recipe_plan['images']),8)
        for row in profile['images']:
            self.assertEqual(recovered_policy(args,row),Path(row['path']).name!='libpython3.12.so.1.0')
        profile['images'][2]['sha256']='0'*64
        self.assertIsNone(resolve_recipe(args,{},profile))
        args.recipe='eager-v12'
        with self.assertRaises(IdentityError):resolve_recipe(args,{},profile)

    def test_private_python_requires_both_original_and_actual_training_identity(self):
        args=argparse.Namespace(recipe='auto');profile=self.fixture();row=profile['images'][0]
        original=dict(row);row.update(path='/private/libpython3.12.so.1.0',sha256='f'*64)
        replacement={'original':original['path'],'optimized':row['path'],'input':original,'output':dict(row)}
        state={'training_images':[replacement]}
        self.assertTrue(resolve_recipe(args,state,profile)['python_source_rebuilt'])
        replacement['output']['sha256']='e'*64
        self.assertIsNone(resolve_recipe(args,state,profile))


if __name__=='__main__':unittest.main()
