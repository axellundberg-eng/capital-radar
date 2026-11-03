from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capital_radar.db import Database
from capital_radar.disk_monitor import DiskMonitor


class DiskMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        Path(".tmp-tests").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=".tmp-tests")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.documents = root / "documents"
        self.documents.mkdir()
        self.database = Database(root / "capital_radar.db")
        self.database.initialize()
        self.monitor = DiskMonitor(
            SimpleNamespace(documents_path=self.documents), self.database,
        )

    def test_local_status_counts_managed_files_instead_of_host_drive(self) -> None:
        payload = b"x" * 4096
        (self.documents / "source.txt").write_bytes(payload)
        with patch.dict(os.environ, {}, clear=True):
            status = self.monitor.get_status()
        self.assertGreaterEqual(status.used_bytes, len(payload))
        self.assertLess(status.used_bytes, 10 * 1024 * 1024)
        self.assertFalse(status.is_over_limit)

    def test_old_manual_runs_use_started_at_column(self) -> None:
        with self.database.connect() as connection:
            issuer_id = connection.execute(
                """INSERT INTO issuers
                   (canonical_name,normalized_name,created_at,updated_at)
                   VALUES ('Testbolaget','testbolaget','2020-01-01','2020-01-01')"""
            ).lastrowid
            connection.execute(
                """INSERT INTO manual_action_runs
                   (issuer_id,action_type,status,phase,started_at)
                   VALUES (?,'interpret_capital_need','completed','completed','2020-01-01')""",
                (issuer_id,),
            )
        self.assertEqual(self.monitor._purge_old_manual_runs(), 1)


if __name__ == "__main__":
    unittest.main()
