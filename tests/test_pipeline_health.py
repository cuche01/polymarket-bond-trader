"""Tests for V4 1.1 PipelineHealth module."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.database import Database
from src.pipeline_health import PipelineHealth


def _config(**overrides):
    cfg = {
        "pipeline_health": {
            "enabled": True,
            "warning_dry_period_hours": 12,
            "critical_dry_period_hours": 18,
            "starvation_dry_period_hours": 36,
            "min_acceptance_rate_24h": 0.005,
            "auto_relaxation": {"enabled": False},
        }
    }
    cfg["pipeline_health"].update(overrides)
    return cfg


class TestPipelineHealth(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = Database(str(self.db_path))
        self.health = PipelineHealth(_config(), self.db)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_record_scan_persists_metrics(self):
        self.health.record_scan({
            "candidates_fetched": 500,
            "candidates_passed_prefilter": 20,
            "candidates_passed_detector": 5,
            "candidates_passed_risk_engine": 2,
            "entries_executed": 1,
            "rejection_reasons": {"detector:price_out_of_band": 480},
            "mode": "paper",
        })
        rows = self.db.execute_read("SELECT * FROM pipeline_health")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidates_fetched"], 500)
        self.assertEqual(rows[0]["entries_executed"], 1)
        self.assertEqual(rows[0]["mode"], "paper")

    def test_acceptance_rate_calculation(self):
        for _ in range(10):
            self.health.record_scan({
                "candidates_fetched": 100,
                "candidates_passed_prefilter": 5,
                "candidates_passed_detector": 2,
                "candidates_passed_risk_engine": 1,
                "entries_executed": 1,
                "mode": "paper",
            })
        rate = self.health.get_acceptance_rate(hours=24)
        self.assertAlmostEqual(rate, 0.01, places=4)

    def test_dry_period_detection_no_entries(self):
        # Backdate a scan with zero entries 20h ago
        past = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
        self.db.execute_write(
            "INSERT INTO pipeline_health "
            "(scan_time, candidates_fetched, candidates_passed_prefilter, "
            " candidates_passed_detector, candidates_passed_risk_engine, "
            " entries_executed, rejection_reasons_json, mode) "
            "VALUES (?, 100, 5, 2, 1, 0, '{}', 'paper')",
            (past,),
        )
        dry = self.health.get_dry_period_hours()
        self.assertGreater(dry, 19)
        self.assertLess(dry, 21)

    def test_starvation_severity_levels(self):
        # 20h dry → CRITICAL
        past = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
        self.db.execute_write(
            "INSERT INTO pipeline_health "
            "(scan_time, candidates_fetched, candidates_passed_prefilter, "
            " candidates_passed_detector, candidates_passed_risk_engine, "
            " entries_executed, rejection_reasons_json, mode) "
            "VALUES (?, 100, 5, 2, 1, 0, '{}', 'paper')",
            (past,),
        )
        severity, action = self.health.check_starvation()
        self.assertEqual(severity, "CRITICAL")
        self.assertEqual(action, "alert")

    def test_starvation_auto_relax_disabled_by_default(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        self.db.execute_write(
            "INSERT INTO pipeline_health "
            "(scan_time, candidates_fetched, candidates_passed_prefilter, "
            " candidates_passed_detector, candidates_passed_risk_engine, "
            " entries_executed, rejection_reasons_json, mode) "
            "VALUES (?, 100, 5, 2, 1, 0, '{}', 'paper')",
            (past,),
        )
        severity, action = self.health.check_starvation()
        self.assertEqual(severity, "STARVATION")
        self.assertEqual(action, "alert")  # not auto_relax (disabled)

    def test_top_rejection_reasons_aggregated(self):
        self.health.record_scan({
            "candidates_fetched": 100,
            "candidates_passed_prefilter": 5,
            "candidates_passed_detector": 2,
            "candidates_passed_risk_engine": 1,
            "entries_executed": 0,
            "rejection_reasons": {"detector:price_out_of_band": 90, "risk:hard_cap": 3},
            "mode": "paper",
        })
        self.health.record_scan({
            "candidates_fetched": 100,
            "candidates_passed_prefilter": 5,
            "candidates_passed_detector": 2,
            "candidates_passed_risk_engine": 1,
            "entries_executed": 0,
            "rejection_reasons": {"detector:price_out_of_band": 88, "risk:hard_cap": 5},
            "mode": "paper",
        })
        top = self.health.get_top_rejection_reasons(limit=2)
        self.assertEqual(top[0][0], "detector:price_out_of_band")
        self.assertEqual(top[0][1], 178)
        self.assertEqual(top[1][0], "risk:hard_cap")
        self.assertEqual(top[1][1], 8)

    def test_24h_summary_structure(self):
        self.health.record_scan({
            "candidates_fetched": 500,
            "candidates_passed_prefilter": 20,
            "candidates_passed_detector": 5,
            "candidates_passed_risk_engine": 2,
            "entries_executed": 1,
            "mode": "paper",
        })
        summary = self.health.get_24h_summary()
        self.assertEqual(summary["entries"], 1)
        self.assertEqual(summary["fetched"], 500)
        self.assertAlmostEqual(summary["acceptance_rate"], 0.002, places=4)

    def test_disabled_health_no_ops(self):
        disabled_cfg = _config()
        disabled_cfg["pipeline_health"]["enabled"] = False
        h = PipelineHealth(disabled_cfg, self.db)
        h.record_scan({"candidates_fetched": 100, "entries_executed": 0})
        rows = self.db.execute_read("SELECT * FROM pipeline_health")
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
