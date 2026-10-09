import os
import tempfile
import time
import unittest
from pathlib import Path

from app.maintenance import BACKUP_GENERATIONS, backup_database, prune_directory
from app.state import StateStore


class MaintenanceTests(unittest.TestCase):
    def test_backup_rotation_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            schema = Path(__file__).resolve().parents[1] / "schema.sql"
            StateStore(state / "chief.sqlite", schema)
            settings = type("SettingsStub", (), {"state_dir": state, "db_path": state / "chief.sqlite"})()
            for index in range(BACKUP_GENERATIONS + 2):
                target = backup_database(settings)
                replacement = target.with_name(f"chief-20000101T0000{index:02d}Z.sqlite")
                target.replace(replacement)
                os.utime(replacement, (time.time() - index, time.time() - index))
            backup_database(settings)
            self.assertLessEqual(len(list((state / "backups").glob("chief-*.sqlite"))), BACKUP_GENERATIONS)

            logs = state / "logs"
            logs.mkdir()
            stale = logs / "stale.log"
            stale.write_text("old", encoding="utf-8")
            os.utime(stale, (time.time() - 9 * 86400, time.time() - 9 * 86400))
            self.assertEqual(prune_directory(logs, 7), 1)
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()

