from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.state import StateStore


DETAIL_LOG_RETENTION_DAYS = 7
EVIDENCE_RETENTION_DAYS = 90
BACKUP_GENERATIONS = 7


def backup_database(settings: Settings) -> Path:
    backup_dir = settings.state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = backup_dir / f"chief-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.sqlite"
    source = sqlite3.connect(settings.db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(target, 0o600)
    backups = sorted(backup_dir.glob("chief-*.sqlite"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in backups[BACKUP_GENERATIONS:]:
        stale.unlink()
    return target


def prune_directory(path: Path, retention_days: int) -> int:
    if not path.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for candidate in path.iterdir():
        if not candidate.is_file() or candidate.stat().st_mtime >= cutoff:
            continue
        candidate.unlink()
        removed += 1
    return removed


def main() -> None:
    settings = Settings.load()
    StateStore(settings.db_path, settings.root / "schema.sql")
    target = backup_database(settings)
    logs_removed = prune_directory(settings.state_dir / "logs", DETAIL_LOG_RETENTION_DAYS)
    evidence_removed = prune_directory(settings.state_dir / "evidence", EVIDENCE_RETENTION_DAYS)
    print(
        f"backup={target.name} logs_removed={logs_removed} evidence_removed={evidence_removed}",
        flush=True,
    )


if __name__ == "__main__":
    main()

