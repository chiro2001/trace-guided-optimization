import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tgo.live.campaign import options,run

class OnlineSelectionPolicy(unittest.TestCase):
    def test_negative_seed_alias_is_not_counted_as_independent_input(self):
        import random
        self.assertEqual(random.Random(101).getstate(),random.Random(-101).getstate())
        parser=argparse.ArgumentParser();options(parser)
        args=parser.parse_args(['-o','/unused','--bundle','/unused','--image','/unused','--bolt','/unused',
            '--python','/unused','--seed','101','--heldout-seed','-101'])
        with self.assertRaisesRegex(ValueError,'规范化'):run(args)

    def test_heldout_regression_returns_baseline_without_trying_another_candidate(self):
        calls=[]
        def measured(_reference,path,configuration,_python,_bolt,pairs,*_args,**_kwargs):
            calls.append((path.name,configuration['seed']))
            gain=-4. if path.name.startswith('heldout') else 15. if path.name.endswith('ext-tsp') else 10.
            validation={'equivalent':True,'requested_pairs':pairs,'completed_pairs':pairs,
                'median_reduction_percent':gain,'min_reduction_percent':gain,'max_reduction_percent':gain,
                'pairs':[{'index':n,'order':['baseline','online'],'baseline':{'wall_ns':1000},
                    'online':{'wall_ns':int(1000*(1-gain/100))},'reduction_percent':gain} for n in range(pairs)]}
            return {'validation':validation,'loaded':{'function_count':100},'hardware_execution_proofs':[{'within_business_roi':True}]}
        with tempfile.TemporaryDirectory() as folder:
            parser=argparse.ArgumentParser();options(parser)
            args=parser.parse_args(['-o',str(Path(folder)/'campaign'),'--bundle','unused','--image','unused',
                '--bolt','unused','--python','unused','--seed','101','--heldout-seed','303','--variant','ext-tsp','--variant','aligned'])
            with patch('tgo.live.campaign.profile_input',return_value={}),patch('tgo.live.campaign.partitions',return_value=(0,[1])),patch('tgo.live.campaign.paired_replay',side_effect=measured):
                result=run(args)
            self.assertEqual(result['state'],'complete');self.assertEqual(result['selected'],'baseline')
            self.assertEqual(result['selected_before_heldout'],'ext-tsp')
            self.assertEqual([name for name,seed in calls if seed==303],['heldout-ext-tsp'])
            self.assertNotIn('confirm-aligned',[name for name,_ in calls])
            self.assertEqual(result['recommendation'],'retain-baseline')

if __name__=='__main__':unittest.main()
