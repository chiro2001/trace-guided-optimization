import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from tgo.jobs import launch,state,stop,freeze_command,frozen_tool_environment
from tgo.util import read_json,write_json

class PersistentJobs(unittest.TestCase):
    def wait(self,directory):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            record=state(directory)
            if record['state'] in ('complete','failed','cancelled','timed_out','resource_limit'):return record
            time.sleep(.03)
        self.fail('持久任务未结束')

    def test_success_and_failure_keep_separate_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for code in [0,7]:
                target=root/str(code)
                launch([sys.executable,'-c',f'import sys;print("job-result");sys.exit({code})'],target)
                result=self.wait(target)
                self.assertEqual(result['returncode'],code)
                self.assertEqual(result['state'],'complete' if code==0 else 'failed')
                self.assertEqual((target/'stdout.log').read_text(),'job-result\n')

    def test_cancel_requires_current_supervisor_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'job'
            launch([sys.executable,'-c','import time;time.sleep(60)'],root)
            deadline=time.monotonic()+2
            while state(root)['state']!='running' and time.monotonic()<deadline:time.sleep(.02)
            original=read_json(root/'job.json');bad=dict(original);bad['supervisor_starttime']+=1
            write_json(root/'job.json',bad)
            with self.assertRaisesRegex(RuntimeError,'身份不符'):stop(root)
            write_json(root/'job.json',original)
            self.assertEqual(stop(root)['state'],'cancellation_requested')
            self.assertEqual(self.wait(root)['state'],'cancelled')

    def test_tgo_job_executes_frozen_source_after_live_source_changes(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);project=root/'project';package=project/'tgo';package.mkdir(parents=True)
            (package/'__init__.py').write_text('')
            cli=package/'cli.py';cli.write_text('def main():\n print("frozen-source")\n return 0\n')
            job=root/'job';job.mkdir()
            command,snapshot=freeze_command([sys.executable,'-m','tgo','--version'],job,project)
            cli.write_text('def main():\n print("mutated-live-source")\n return 0\n')
            output=subprocess.check_output(command,cwd=project,text=True)
            self.assertEqual(output,'frozen-source\n')
            self.assertTrue(Path(snapshot['manifest']).is_file())

    def test_cancel_cleans_grandchild_in_another_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);pid_file=root/'grandchild.pid'
            child='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
            parent='import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c",'+repr(child)+'],start_new_session=True);Path('+repr(str(pid_file))+').write_text(str(p.pid));time.sleep(60)'
            job=root/'job';launch([sys.executable,'-c',parent],job)
            deadline=time.monotonic()+3
            while not pid_file.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(pid_file.is_file());pid=int(pid_file.read_text())
            stop(job);self.assertEqual(self.wait(job)['state'],'cancelled')
            path=Path(f'/proc/{pid}/stat')
            if path.exists():self.assertEqual(path.read_text().rsplit(')',1)[1].split()[0],'Z')

    def test_resource_floor_prevents_starting_the_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);marker=root/'payload-ran'
            launch([sys.executable,'-c','from pathlib import Path;Path('+repr(str(marker))+').touch()'],root/'job',mem_floor_gib=1000000)
            self.assertEqual(self.wait(root/'job')['state'],'resource_limit')
            self.assertFalse(marker.exists())

    def test_waiting_job_runs_only_after_predecessor_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);first=root/'first';second=root/'second';flag=root/'ran'
            launch([sys.executable,'-c','import time;time.sleep(.5)'],first)
            launch([sys.executable,'-c','from pathlib import Path;Path('+repr(str(flag))+').touch()'],second,wait_for=[first])
            self.assertEqual(state(second)['state'],'waiting');self.assertFalse(flag.exists())
            self.assertEqual(self.wait(second)['state'],'complete');self.assertTrue(flag.exists())
            self.assertEqual(state(first)['state'],'complete')
            self.assertNotIn('waiting_for',state(second))

    def test_private_dependency_paths_survive_source_freeze(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);binary=root/'deps/bin/llvm-bolt';binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
            with patch.dict(os.environ,{'PATH':'/usr/bin'},clear=True):
                environment,resolved=frozen_tool_environment({},root)
                self.assertEqual(environment['TGO_LLVM_BOLT'],str(binary))
                self.assertEqual(resolved['TGO_LLVM_BOLT'],str(binary))
                explicit,_=frozen_tool_environment({'TGO_LLVM_BOLT':'/chosen/tool'},root)
                self.assertEqual(explicit['TGO_LLVM_BOLT'],'/chosen/tool')

    def test_explicit_tool_is_never_silently_replaced_by_another_version(self):
        from tgo.util import tool
        with self.assertRaisesRegex(RuntimeError,'指定工具不可执行'):tool('cc','/no-such-explicit-compiler')
        self.assertIsNone(tool('cc','/no-such-explicit-compiler',required=False))

if __name__=='__main__':unittest.main()
