import platform
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tgo.util import tool,read_json


class QEMUWindows(unittest.TestCase):
    def test_record_limit_keeps_workload_running_through_both_regions(self):
        qemu=tool('qemu-'+platform.machine(),required=False)
        project=Path(__file__).resolve().parents[1]
        header=Path(os.environ.get('TGO_QEMU_HEADER',str(project/'deps/install/include/qemu-plugin.h')))
        if not qemu or not header.exists():self.skipTest('matching QEMU SDK required')
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'capture'
            code='from tgo.roi import region\nfor i in range(2):\n with region("test"):\n  value=sum(range(1000))\n print(i,value)\n'
            run=subprocess.run([sys.executable,'-m','tgo','record','-o',str(output),'--backend','qemu',
                '--qemu',qemu,'--qemu-header',str(header),'--roi','marker','--max-roi-windows','1',
                '--',sys.executable,'-c',code],cwd=project,capture_output=True,text=True,timeout=90)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual((output/'stdout').read_text().splitlines(),['0 499500','1 499500'])
            profile=read_json(output/'profile.json')
            self.assertEqual(profile['marker_windows'],1)
            self.assertEqual(profile['quality']['roi_window_limit'],1)
            markers=''.join(p.read_text() for p in output.glob('capture.*.roi'))
            self.assertEqual(sum(line.startswith('B\t') for line in markers.splitlines()),2)

    def test_taskset_exec_wrapper_does_not_lose_the_emulated_roi(self):
        qemu=tool('qemu-'+platform.machine(),required=False)
        project=Path(__file__).resolve().parents[1]
        header=Path(os.environ.get('TGO_QEMU_HEADER',str(project/'deps/install/include/qemu-plugin.h')))
        if not qemu or not header.exists():self.skipTest('matching QEMU SDK required')
        cpus=','.join(map(str,sorted(os.sched_getaffinity(0))[:2]))
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'capture'
            command=[sys.executable,'-m','tgo','record','-o',str(output),'--backend','qemu','--qemu',qemu,
                '--qemu-header',str(header),'--roi','marker','--','taskset','-c',cpus,sys.executable,'-c',
                'from tgo.roi import region\nwith region():\n print(sum(range(1000)))\n']
            result=subprocess.run(command,cwd=project,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertGreater(read_json(output/'profile.json')['instructions'],0)
            capture=read_json(output/'run.json')
            self.assertEqual(capture['cpus'],cpus)
            self.assertEqual(capture['command'][0],sys.executable)
            self.assertEqual(capture['launch_adapters'][0]['kind'],'taskset-cpu-list')


if __name__=='__main__':unittest.main()
