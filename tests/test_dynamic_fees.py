"""Tests for V4 1.2 dynamic fee model."""

import unittest

from src.utils import (
    calculate_maker_fee,
    calculate_taker_fee,
    estimate_round_trip_fee_rate,
    fee_schedule_from_category,
    resolve_fee_schedule,
)


class TestTakerFee(unittest.TestCase):
    def test_taker_fee_symmetric_around_half(self):
        # Polymarket formula: fee = shares × Θ × p × (1-p). p(1-p) peaks at 0.5.
        schedule = {"feesEnabled": True, "takerFeeCoefficient": 0.01}
        fee_at_half = calculate_taker_fee(0.50, 1000, schedule)
        fee_at_low = calculate_taker_fee(0.05, 1000, schedule)
        fee_at_high = calculate_taker_fee(0.95, 1000, schedule)
        self.assertAlmostEqual(fee_at_low, fee_at_high, places=6)
        self.assertGreater(fee_at_half, fee_at_low)
        # At p=0.5, fee = shares × Θ × 0.25
        self.assertAlmostEqual(fee_at_half, 1000 * 0.01 * 0.25, places=6)

    def test_maker_fee_always_zero(self):
        schedule = {"feesEnabled": True, "takerFeeCoefficient": 0.018}
        self.assertEqual(calculate_maker_fee(0.50, 10000, schedule), 0.0)
        self.assertEqual(calculate_maker_fee(0.96, 500, schedule), 0.0)
        self.assertEqual(calculate_maker_fee(0.96, 500, None), 0.0)

    def test_geopolitics_no_fee(self):
        # Geopolitics has 0% peak taker rate → fee = 0 everywhere.
        schedule = fee_schedule_from_category("Geopolitics")
        self.assertFalse(schedule["feesEnabled"])
        self.assertEqual(calculate_taker_fee(0.96, 100, schedule), 0.0)
        rtt = estimate_round_trip_fee_rate(
            entry_price=0.96,
            exit_price=0.96,
            fee_schedule=schedule,
            entry_is_maker=False,
            exit_is_taker=True,
        )
        self.assertEqual(rtt, 0.0)


class TestRoundTripFeeRate(unittest.TestCase):
    def test_round_trip_respects_resolution_flag(self):
        # Resolution (YES redemption at $1.00) is fee-free → exit leg = 0.
        schedule = {"feesEnabled": True, "takerFeeCoefficient": 0.01}
        rate_taker_exit = estimate_round_trip_fee_rate(
            entry_price=0.96,
            exit_price=None,
            fee_schedule=schedule,
            entry_is_maker=True,
            exit_is_taker=True,
            exit_is_resolution=False,
        )
        rate_resolution = estimate_round_trip_fee_rate(
            entry_price=0.96,
            exit_price=None,
            fee_schedule=schedule,
            entry_is_maker=True,
            exit_is_taker=True,
            exit_is_resolution=True,
        )
        self.assertGreater(rate_taker_exit, 0)
        self.assertEqual(rate_resolution, 0.0)

    def test_fallback_when_fee_schedule_missing(self):
        # Config says use_dynamic_fees=true but market has no feeSchedule; the
        # resolver returns a category-derived schedule. For unknown category,
        # fallback_taker_rate is used.
        market = {"category": "Weather"}
        fees_cfg = {"use_dynamic_fees": True, "fallback_taker_rate": 0.002}
        fs = resolve_fee_schedule(market, fees_cfg)
        self.assertIsNotNone(fs)
        self.assertTrue(fs["feesEnabled"])
        # Weather peak = 0.0125 per category table
        self.assertAlmostEqual(fs["takerFeeCoefficient"], 0.0125, places=6)

        # When dynamic fees disabled, resolver returns None (legacy behaviour).
        fees_cfg_off = {"use_dynamic_fees": False, "fallback_taker_rate": 0.002}
        self.assertIsNone(resolve_fee_schedule(market, fees_cfg_off))


class TestNetYieldGate(unittest.TestCase):
    def test_net_yield_gate_with_dynamic_fees(self):
        from src.detector import PseudoCertaintyDetector

        config = {
            "scanner": {"min_entry_price": 0.90, "max_entry_price": 0.99},
            "risk": {"min_net_yield": 0.015},
            "fees": {
                "use_dynamic_fees": True,
                "fallback_taker_rate": 0.002,
                "assume_entry_maker": True,
                "assume_exit_taker": True,
            },
            "feature_flags": {},
            "detector": {"min_parity_sum": 0.96, "max_parity_sum": 1.04},
        }
        detector = PseudoCertaintyDetector(config)

        # Geopolitics at $0.96 → fee-free, gross yield ≈ 4.17%, net ≈ 4.17%
        # → should PASS the 1.5% min yield gate.
        fs_geo = fee_schedule_from_category("Geopolitics")
        from src.utils import estimate_round_trip_fee_rate
        geo_rate = estimate_round_trip_fee_rate(
            entry_price=0.96, exit_price=None, fee_schedule=fs_geo,
            entry_is_maker=True, exit_is_taker=True, exit_is_resolution=False,
        )
        gross = (1 - 0.96) / 0.96
        self.assertGreaterEqual(gross - geo_rate, 0.015)

        # Crypto at $0.985 → fee-bearing exit, gross = (0.015/0.985) ≈ 1.52%,
        # exit fee rate at $1.00 (clamped to 0.99) is 0.018 × 0.99 × 0.01 ≈
        # 0.000178 → net ≈ 1.50% — right at the cliff. Push entry up to 0.988
        # and the gate must fail.
        fs_crypto = fee_schedule_from_category("Crypto")
        rate_at_988 = estimate_round_trip_fee_rate(
            entry_price=0.988, exit_price=None, fee_schedule=fs_crypto,
            entry_is_maker=True, exit_is_taker=True, exit_is_resolution=False,
        )
        gross_988 = (1 - 0.988) / 0.988
        self.assertLess(gross_988 - rate_at_988, 0.015)


if __name__ == "__main__":
    unittest.main()
