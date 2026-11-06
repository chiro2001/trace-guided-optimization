import subprocess
import sys
import unittest

class SemanticGate(unittest.TestCase):
    def test_optimized_python_mode_does_not_remove_gate_checks(self):
        source='''from types import SimpleNamespace
from tgo.live.semantic import run
context=SimpleNamespace()
first=run(context);second=run(context)
if first!=second or first['checks']<320:raise RuntimeError('语义检查未完整执行')
class BrokenPattern:
 def fullmatch(self,text):return None
context.semantic_patterns[0]=BrokenPattern()
try:run(context)
except RuntimeError:print('gate-rejected-broken-semantics')
else:raise RuntimeError('语义错误未被门禁检出')
'''
        result=subprocess.run([sys.executable,'-O','-c',source],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout,'gate-rejected-broken-semantics\n')

if __name__=='__main__':unittest.main()
