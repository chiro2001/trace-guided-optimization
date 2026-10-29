from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from tgo.util import run


class ProcessCleanup(unittest.TestCase):
    def test_build_timeout_kills_descendant_that_ignores_sigterm(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); pidfile = root / "child.pid"
            child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            parent = "import subprocess,time;from pathlib import Path;p=subprocess.Popen(" + repr([sys.executable,"-c",child]) + ");Path(" + repr(str(pidfile)) + ").write_text(str(p.pid));time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                run([sys.executable,"-c",parent],root/"tool.log",timeout=0.5)
            pid=int(pidfile.read_text())
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                stat=Path(f"/proc/{pid}/stat")
                if not stat.exists() or stat.read_text().rsplit(") ",1)[1].split()[0]=="Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("timed-out build left its descendant running")


if __name__ == "__main__":
    unittest.main()
