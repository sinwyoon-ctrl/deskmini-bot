import gc
import tempfile
import unittest
from pathlib import Path

from probe_claude_orca import finish_fixture, released_worker, reserve_report


class ProbeSafety(unittest.TestCase):
    def test_empty_or_other_worker_cannot_prove_release(self):
        self.assertFalse(released_worker([],'expected'))
        self.assertFalse(released_worker([{'dispatchId':'other','terminalState':'released'}],'expected'))
        self.assertFalse(released_worker([{'dispatchId':'expected','terminalState':'active'},
                                         {'dispatchId':'other','terminalState':'released'}],'expected'))
        self.assertTrue(released_worker([{'dispatchId':'expected','terminalState':'released'}],'expected'))

    def test_failure_after_task_settlement_preserves_fixture_evidence(self):
        class Fixture:
            def __init__(self):
                self.temp=tempfile.TemporaryDirectory()
                self.home=Path(self.temp.name)
                (self.home/'db-evidence').write_text('native task succeeded; later verification failed')
            def doCleanups(self):
                pass
            def tearDown(self):
                self.temp.cleanup()
        for failure in ('result verification failed','coordinator close failed'):
            fixture=Fixture(); root=fixture.home
            finish_fixture(fixture,False)
            del fixture
            gc.collect()
            self.assertTrue((root/'db-evidence').exists(),failure)
            (root/'db-evidence').unlink(); root.rmdir()

    def test_repeated_report_name_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            report=Path(folder)/'result.json'
            resources=reserve_report(report)
            resources.write_text('preserved authoritative launch receipt')
            with self.assertRaises(FileExistsError):
                reserve_report(report)
            self.assertEqual(resources.read_text(),'preserved authoritative launch receipt')
