from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from capital_radar.config import Settings


class TimeoutBoundaryTests(unittest.TestCase):
    def test_default_timeout_order_is_strict(self) -> None:
        with (
            patch("capital_radar.config._load_local_env"),
            patch.dict(os.environ, {}, clear=True),
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.http_timeout, 30.0)
        self.assertEqual(settings.extraction_hard_timeout, 240.0)
        self.assertEqual(settings.action_timeout, 300.0)
        self.assertLess(settings.http_timeout, settings.extraction_hard_timeout)
        self.assertLess(settings.extraction_hard_timeout, settings.action_timeout)
        self.assertEqual(settings.mfn_monitor_interval_seconds, 3600)
        self.assertEqual(settings.analysis_worker_threads, 2)
        self.assertEqual(settings.deepseek_flash_max_concurrency, 4)


if __name__ == "__main__":
    unittest.main()
