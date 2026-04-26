"""Tests for V4 Phase 2 — candidate-universe expansion.

Covers:
- 2.1 Category-specific min_net_yield gate (detector)
- 2.2 Category-specific entry bands (scanner)
- 2.3 Holding-rewards estimation + bond-score boost + reconciliation
- 2.4 Resolution-date clustering (risk_engine + database + portfolio)
- 2.5 LP-rewards preference (scanner + bond score)
- 2.6 Shadow-mode plumbing
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.database import Database
from src.detector import PseudoCertaintyDetector
from src.portfolio_manager import PortfolioManager
from src.rewards_reconciler import RewardsReconciler
from src.risk_engine import RiskEngine
from src.scanner import MarketScanner
from src.utils import (
    calculate_bond_score,
    estimate_holding_rewards,
    feature_enabled,
    shadow_enabled,
)


def _base_config(**overrides):
    cfg = {
        "scanner": {
            "min_entry_price": 0.94,
            "max_entry_price": 0.98,
            "min_liquidity": 1000,
            "min_volume_24h": 500,
            "max_price_volatility_1d": 0.05,
            "excluded_categories": [],
        },
        "risk": {
            "min_net_yield": 0.015,
            "max_single_market_pct": 0.04,
            "target_position_pct": 0.03,
            "max_correlated_pct": 0.15,
            "max_category_exposure_pct": 0.20,
            "max_deployed_pct": 0.70,
            "max_daily_loss_pct": 0.02,
            "consecutive_loss_halt": 3,
            "max_slippage_pct": 0.02,
            "volume_size_max_pct": 0.02,
            "min_viable_position": 15.0,
            "max_positions_per_underlying": 0,
            "underlying_cooldown_hours": 0,
            "resolution_date_cluster_pct": 0.25,
            "resolution_date_cluster_window_hours": 24,
        },
        "fees": {"assume_entry_maker": True, "assume_exit_taker": True},
        "orderbook": {},
        "detector": {},
        "binary_catalyst": {},
        "feature_flags": {},
        "holding_rewards": {"apy": 0.04, "reconciliation_enabled": False},
        "lp_rewards": {"score_boost": 1.15, "min_daily_rate": 100},
    }
    for k, v in overrides.items():
        section, _, key = k.partition(".")
        if key:
            cfg.setdefault(section, {})[key] = v
        else:
            cfg[section] = v
    return cfg


# ─── 2.6 Shadow-mode plumbing ────────────────────────────────────────────────


class TestShadowEnabled(unittest.TestCase):
    def test_shadow_flag_independent_of_real_flag(self):
        cfg = {"feature_flags": {"foo": False, "shadow_foo": True}}
        self.assertFalse(feature_enabled(cfg, "foo"))
        self.assertTrue(shadow_enabled(cfg, "foo"))

    def test_shadow_defaults_false(self):
        self.assertFalse(shadow_enabled({}, "anything"))
        self.assertFalse(shadow_enabled({"feature_flags": {}}, "anything"))


# ─── 2.1 Category-specific min_net_yield ─────────────────────────────────────


class TestCategoryMinYield(unittest.TestCase):
    def test_resolve_returns_none_when_unconfigured(self):
        det = PseudoCertaintyDetector(_base_config())
        self.assertIsNone(det._resolve_category_min_yield("Crypto"))

    def test_resolve_returns_category_override(self):
        cfg = _base_config()
        cfg["risk"]["min_net_yield_by_category"] = {"Crypto": 0.020, "_unknown": 0.012}
        det = PseudoCertaintyDetector(cfg)
        self.assertAlmostEqual(det._resolve_category_min_yield("Crypto"), 0.020)

    def test_resolve_falls_back_to_unknown(self):
        cfg = _base_config()
        cfg["risk"]["min_net_yield_by_category"] = {"Crypto": 0.020, "_unknown": 0.012}
        det = PseudoCertaintyDetector(cfg)
        self.assertAlmostEqual(det._resolve_category_min_yield("Random"), 0.012)

    def test_hard_floor_clamps_misconfigured_value(self):
        cfg = _base_config(**{"risk.min_net_yield": 0.015})
        # Misconfig: setting floor below (min_net_yield - 0.005) should clamp.
        cfg["risk"]["min_net_yield_by_category"] = {"Crypto": 0.001}
        det = PseudoCertaintyDetector(cfg)
        # 0.015 - 0.005 = 0.010 → floor, not the 0.001 setting.
        self.assertAlmostEqual(det._resolve_category_min_yield("Crypto"), 0.010)


# ─── 2.2 Category-specific entry bands ───────────────────────────────────────


class TestEntryBands(unittest.TestCase):
    def test_resolve_returns_global_when_unconfigured(self):
        sc = MarketScanner(_base_config())
        lo, hi = sc._resolve_entry_band("Crypto")
        self.assertAlmostEqual(lo, 0.94)
        self.assertAlmostEqual(hi, 0.98)

    def test_resolve_returns_category_override(self):
        cfg = _base_config()
        cfg["scanner"]["entry_band_by_category"] = {
            "Geopolitics": {"min": 0.93, "max": 0.985}
        }
        sc = MarketScanner(cfg)
        lo, hi = sc._resolve_entry_band("Geopolitics")
        self.assertAlmostEqual(lo, 0.93)
        self.assertAlmostEqual(hi, 0.985)

    def test_shadow_mode_rescues_widened_band(self):
        cfg = _base_config()
        cfg["scanner"]["entry_band_by_category"] = {
            "Geopolitics": {"min": 0.93, "max": 0.985}
        }
        cfg["feature_flags"] = {
            "category_entry_bands": False,
            "shadow_category_entry_bands": True,
        }
        sc = MarketScanner(cfg)
        end = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        markets = [{
            "active": True,
            "closed": False,
            "outcomePrices": '["0.935","0.065"]',
            "clobTokenIds": '["yes123","no456"]',
            "endDate": end,
            "liquidityClob": 5000,
            "volume24hr": 2000,
            "oneDayPriceChange": 0.005,
            "category": "Geopolitics",
            "question": "Will a Geopolitics event happen?",
        }]
        candidates = sc.filter_candidates(markets)
        # In shadow mode, 0.935 is below global min (0.94) so it's still
        # rejected — but the shadow-rescue counter should have been bumped.
        self.assertEqual(len(candidates), 0)

    def test_enforce_mode_widens_band(self):
        cfg = _base_config()
        cfg["scanner"]["entry_band_by_category"] = {
            "Geopolitics": {"min": 0.93, "max": 0.985}
        }
        cfg["feature_flags"] = {"category_entry_bands": True}
        sc = MarketScanner(cfg)
        end = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        markets = [{
            "active": True,
            "closed": False,
            "outcomePrices": '["0.935","0.065"]',
            "clobTokenIds": '["yes123","no456"]',
            "endDate": end,
            "liquidityClob": 5000,
            "volume24hr": 2000,
            "oneDayPriceChange": 0.005,
            "category": "Geopolitics",
            "question": "Will a Geopolitics event happen?",
        }]
        candidates = sc.filter_candidates(markets)
        self.assertEqual(len(candidates), 1)


# ─── 2.3 Holding rewards ─────────────────────────────────────────────────────


class TestHoldingRewardsEstimate(unittest.TestCase):
    def test_simple_interest_formula(self):
        # $1000 × 4% × 10/365 = $1.0959
        actual = estimate_holding_rewards(1000, 10, 0.04)
        self.assertAlmostEqual(actual, 1000 * 0.04 * 10 / 365, places=4)

    def test_zero_inputs_return_zero(self):
        self.assertEqual(estimate_holding_rewards(0, 10, 0.04), 0.0)
        self.assertEqual(estimate_holding_rewards(1000, 0, 0.04), 0.0)
        self.assertEqual(estimate_holding_rewards(1000, 10, 0), 0.0)


class TestBondScoreRewardsBoost(unittest.TestCase):
    def test_reward_eligible_market_boosts_score(self):
        base = calculate_bond_score(
            entry_price=0.96,
            days_to_resolution=7,
            liquidity_clob=20000,
            one_day_price_change=0.005,
        )
        boosted = calculate_bond_score(
            entry_price=0.96,
            days_to_resolution=7,
            liquidity_clob=20000,
            one_day_price_change=0.005,
            holding_rewards_enabled=True,
            holding_rewards_apr=0.04,
        )
        self.assertGreater(boosted, base)
        # 4% APY × 7/365 ≈ 0.00077 additive yield → few-percent boost.
        ratio = boosted / base
        self.assertGreater(ratio, 1.005)
        self.assertLess(ratio, 1.10)

    def test_lp_rewards_boost_multiplicative(self):
        base = calculate_bond_score(
            entry_price=0.96,
            days_to_resolution=7,
            liquidity_clob=20000,
            one_day_price_change=0.005,
        )
        lp = calculate_bond_score(
            entry_price=0.96,
            days_to_resolution=7,
            liquidity_clob=20000,
            one_day_price_change=0.005,
            lp_rewards_boost=1.15,
        )
        self.assertAlmostEqual(lp, base * 1.15, places=6)

    def test_non_eligible_score_unchanged(self):
        base = calculate_bond_score(
            entry_price=0.96,
            days_to_resolution=7,
            liquidity_clob=20000,
            one_day_price_change=0.005,
        )
        same = calculate_bond_score(
            entry_price=0.96,
            days_to_resolution=7,
            liquidity_clob=20000,
            one_day_price_change=0.005,
            holding_rewards_enabled=False,
            lp_rewards_boost=1.0,
        )
        self.assertAlmostEqual(base, same)


class TestScannerAttachesRewards(unittest.TestCase):
    def test_attach_rewards_enabled_market(self):
        sc = MarketScanner(_base_config())
        market = {
            "holdingRewardsEnabled": True,
            "clobRewards": [{"rewardsDailyRate": 500}],
        }
        sc._attach_rewards_fields(market)
        self.assertTrue(market["_holding_rewards_enabled"])
        self.assertAlmostEqual(market["_holding_rewards_apr"], 0.04)
        self.assertTrue(market["_lp_rewards_enabled"])
        self.assertAlmostEqual(market["_lp_rewards_daily_rate"], 500.0)

    def test_attach_rewards_disabled_market(self):
        sc = MarketScanner(_base_config())
        market = {"holdingRewardsEnabled": False, "clobRewards": []}
        sc._attach_rewards_fields(market)
        self.assertFalse(market["_holding_rewards_enabled"])
        self.assertEqual(market["_holding_rewards_apr"], 0.0)
        self.assertFalse(market["_lp_rewards_enabled"])

    def test_lp_reward_below_floor_not_enabled(self):
        sc = MarketScanner(_base_config())
        market = {
            "holdingRewardsEnabled": False,
            "clobRewards": [{"rewardsDailyRate": 50}],  # under 100 floor
        }
        sc._attach_rewards_fields(market)
        self.assertFalse(market["_lp_rewards_enabled"])


# ─── 2.3e Rewards reconciliation ─────────────────────────────────────────────


class TestRewardsReconciliation(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_paper_mode_populates_actual_from_estimate(self):
        # Insert a reward-eligible position opened 5 days ago.
        entry_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        pos_id = self.db.save_position({
            "market_id": "m1",
            "market_question": "test",
            "token_id": "t1",
            "entry_price": 0.96,
            "shares": 100,
            "cost_basis": 96.0,
            "entry_time": entry_time,
            "expected_resolution": entry_time,
            "paper_trade": True,
        })
        self.assertTrue(pos_id)
        self.db.update_position(pos_id, {
            "holding_rewards_enabled": 1,
            "holding_rewards_apr": 0.04,
        })

        rec = RewardsReconciler(
            config={"holding_rewards": {"apy": 0.04, "reconciliation_enabled": False}},
            db=self.db,
            paper_mode=True,
        )
        updated = asyncio.run(rec.reconcile())
        self.assertEqual(updated, 1)

        row = self.db.get_position_by_id(pos_id)
        self.assertIsNotNone(row.get("actual_holding_rewards"))
        # 96 × 0.04 × 5/365 ≈ 0.0526
        self.assertAlmostEqual(row["actual_holding_rewards"], 96 * 0.04 * 5 / 365, places=3)

    def test_skips_non_eligible_positions(self):
        entry_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.db.save_position({
            "market_id": "m2",
            "market_question": "test",
            "token_id": "t2",
            "entry_price": 0.96,
            "shares": 100,
            "cost_basis": 96.0,
            "entry_time": entry_time,
            "expected_resolution": entry_time,
            "paper_trade": True,
        })
        rec = RewardsReconciler(
            config={"holding_rewards": {"apy": 0.04, "reconciliation_enabled": False}},
            db=self.db,
            paper_mode=True,
        )
        updated = asyncio.run(rec.reconcile())
        self.assertEqual(updated, 0)


# ─── 2.4 Resolution-date clustering ──────────────────────────────────────────


class TestResolutionDateExposure(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _mkpos(self, *, resolution_iso, cost_basis, market_id="m"):
        return self.db.save_position({
            "market_id": market_id,
            "market_question": "q",
            "token_id": "t",
            "entry_price": 0.96,
            "shares": 100,
            "cost_basis": cost_basis,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "expected_resolution": resolution_iso,
            "paper_trade": True,
        })

    def test_sums_positions_inside_window(self):
        anchor = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        self._mkpos(resolution_iso=(anchor + timedelta(hours=3)).isoformat(), cost_basis=100)
        self._mkpos(resolution_iso=(anchor - timedelta(hours=5)).isoformat(), cost_basis=200)
        self._mkpos(resolution_iso=(anchor + timedelta(hours=20)).isoformat(), cost_basis=400)
        exposure = self.db.get_resolution_date_exposure(
            anchor.isoformat(), window_hours=24, paper_trade=True
        )
        # All three are within ±12h of anchor? First is +3h ✓, second is -5h ✓,
        # third is +20h → outside ±12h window → excluded.
        self.assertAlmostEqual(exposure, 300.0)

    def test_unparseable_time_returns_zero(self):
        exposure = self.db.get_resolution_date_exposure("not-a-date", paper_trade=True)
        self.assertEqual(exposure, 0.0)


class TestResolutionClusterCheck(unittest.TestCase):
    def _engine(self, cluster_flag=True, shadow=False, existing_exposure=0.0, balance=1000.0):
        cfg = _base_config()
        cfg["feature_flags"] = {
            "resolution_date_cluster": cluster_flag,
            "shadow_resolution_date_cluster": shadow,
        }
        portfolio = MagicMock()
        portfolio.get_portfolio_balance.return_value = balance
        portfolio.get_resolution_date_exposure.return_value = existing_exposure
        portfolio.db = MagicMock()
        portfolio.db.get_open_positions.return_value = []
        return RiskEngine(cfg, portfolio)

    def test_rejects_over_25pct(self):
        engine = self._engine(existing_exposure=200.0, balance=1000.0)
        ok, reason = engine.check_resolution_date_cluster(
            "2026-05-01T12:00:00Z", requested_size=100.0, portfolio_balance=1000.0
        )
        self.assertFalse(ok)
        self.assertIn("cluster", reason.lower())

    def test_allows_under_25pct(self):
        engine = self._engine(existing_exposure=100.0, balance=1000.0)
        ok, _ = engine.check_resolution_date_cluster(
            "2026-05-01T12:00:00Z", requested_size=100.0, portfolio_balance=1000.0
        )
        self.assertTrue(ok)

    def test_no_enforcement_when_flag_off(self):
        engine = self._engine(cluster_flag=False, shadow=False, existing_exposure=900.0)
        ok, _ = engine.check_resolution_date_cluster(
            "2026-05-01T12:00:00Z", requested_size=100.0, portfolio_balance=1000.0
        )
        self.assertTrue(ok)

    def test_shadow_mode_logs_but_allows(self):
        engine = self._engine(cluster_flag=False, shadow=True, existing_exposure=900.0)
        ok, _ = engine.check_resolution_date_cluster(
            "2026-05-01T12:00:00Z", requested_size=100.0, portfolio_balance=1000.0
        )
        self.assertTrue(ok)

    def test_missing_resolution_time_passes(self):
        engine = self._engine(existing_exposure=900.0)
        ok, _ = engine.check_resolution_date_cluster(
            None, requested_size=100.0, portfolio_balance=1000.0
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
