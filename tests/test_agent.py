import json
import subprocess
import sys
import unittest

class AgentProtocol(unittest.TestCase):
    def test_stdio_handshake_and_structural_tool_use_real_process(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'tgo_capabilities','arguments':{'image':sys.executable}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'tgo_capabilities','arguments':{}}}]
        process=subprocess.run([sys.executable,'-m','tgo','agent'],input=''.join(json.dumps(x)+'\n' for x in messages),
            capture_output=True,text=True,timeout=15,check=True)
        replies=[json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([x['id'] for x in replies],[1,2,3,4])
        self.assertEqual(replies[0]['result']['protocolVersion'],'2025-06-18')
        self.assertIn('tgo_job_stop',{t['name'] for t in replies[1]['result']['tools']})
        self.assertIn('tgo_live_python_start',{t['name'] for t in replies[1]['result']['tools']})
        self.assertFalse(replies[2]['result']['structuredContent']['known_input_allowlist'])
        self.assertTrue(replies[3]['result']['isError'])
        self.assertEqual(process.stderr,'')

    def test_invalid_params_and_nonfinite_numbers_do_not_crash_stdio(self):
        requests=[{'jsonrpc':'2.0','id':1,'method':'tools/call','params':[]},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'tgo_job_start','arguments':{'command':['true'],'output':'/unused','timeout':float('inf')}}},
            {'jsonrpc':'2.0','id':3,'method':'ping'}]
        process=subprocess.run([sys.executable,'-m','tgo','agent'],input=''.join(json.dumps(x)+'\n' for x in requests),
            capture_output=True,text=True,timeout=15,check=True)
        replies=[json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(replies[0]['error']['code'],-32602)
        self.assertEqual(replies[1]['error']['code'],-32700)
        self.assertEqual(replies[2]['result'],{})

    def test_online_tool_rejects_unknown_workload_before_creating_job(self):
        from tgo.agent import call
        with self.assertRaisesRegex(ValueError,'可用值'):
            call('tgo_live_python_start',{'job_output':'/unused','output':'/unused','bundle':'/unused',
                'image':'/unused','bolt':'/unused','python':sys.executable,'case':'unknown-workload'})

    def test_online_mode_fields_are_checked_before_creating_job(self):
        from tgo.agent import call
        base={'job_output':'/unused','output':'/unused','bundle':'/unused','image':'/unused','bolt':'/unused','python':sys.executable}
        for fields in [{'mode':'tune','layout':'aligned'},{'mode':'replay','heldout_seed':303},
                       {'mode':'tune','variant':['unknown']},{'capsule':'true'},
                       {'workload':'python-stdlib','case':'mixed'},{'workload':'python-heavy','modules':8}]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):call('tgo_live_python_start',{**base,**fields})

if __name__=='__main__':unittest.main()
