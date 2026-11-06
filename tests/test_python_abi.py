import sys
import unittest
from tgo.cpython import interpreter_info,verify_abi

class InterpreterABI(unittest.TestCase):
    def test_real_interpreter_probe_rejects_changed_runtime_abi(self):
        original=interpreter_info(sys.executable)
        self.assertEqual(verify_abi(sys.executable,original)['version'],original['version'])
        altered={**original,'gil_disabled':1-original['gil_disabled']}
        with self.assertRaisesRegex(ValueError,'gil_disabled'):verify_abi(sys.executable,altered)

if __name__=='__main__':unittest.main()
