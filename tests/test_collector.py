import os
import sys
import time
import unittest

from app.collector import run_fixed


@unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX-only")
class CollectorTests(unittest.TestCase):
    def test_timeout_kills_sigterm_ignoring_child_and_returns(self) -> None:
        child = (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "time.sleep(30)"
        )
        started = time.monotonic()
        result = run_fixed((sys.executable, "-c", parent), timeout=0.2)
        self.assertEqual(result.returncode, 124)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
