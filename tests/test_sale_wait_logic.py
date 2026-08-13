from __future__ import annotations

from datetime import datetime
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from damai_checkout import (  # noqa: E402
    CountdownSignalGate,
    PURCHASE_RETRY_INTERVAL_S,
    parse_args,
    parse_page_sale_time,
    seconds_until_timer,
    validate_page_sale_time,
)


class SaleTimeParsingTests(unittest.TestCase):
    def test_parses_zero_padded_time(self) -> None:
        self.assertEqual(
            parse_page_sale_time("08月18日 13:00开抢"),
            (8, 18, 13, 0),
        )

    def test_parses_compact_time(self) -> None:
        self.assertEqual(
            parse_page_sale_time("8月12日15:33开抢"),
            (8, 12, 15, 33),
        )

    def test_rejects_mismatched_command_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "不一致"):
            validate_page_sale_time(
                "08月18日 13:00开抢",
                datetime(2026, 8, 18, 15, 0),
            )

    def test_fast_requires_sale_time(self) -> None:
        argv = [
            "damai_checkout.py",
            "--profile",
            "missing.json",
            "--serial",
            "test",
            "--wait-for-sale",
            "--fast",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch("sys.stderr", new_callable=StringIO),
            self.assertRaises(SystemExit) as error,
        ):
            parse_args()
        self.assertEqual(error.exception.code, 2)

    def test_timer_is_immediate_when_fallback_deadline_already_passed(self) -> None:
        self.assertEqual(seconds_until_timer(-0.2, 0.15), 0.0)

    def test_timer_includes_fallback_delay_before_sale(self) -> None:
        self.assertAlmostEqual(seconds_until_timer(2.0, 0.15), 2.15)

    def test_purchase_retry_interval_is_100ms(self) -> None:
        self.assertEqual(PURCHASE_RETRY_INTERVAL_S, 0.1)


class CountdownSignalGateTests(unittest.TestCase):
    def test_refresh_miss_cannot_trigger_until_countdown_reappears(self) -> None:
        gate = CountdownSignalGate(confirm_count=2)
        gate.disarm()

        self.assertFalse(gate.observe(False, 1.00, signal_allowed=True))
        self.assertFalse(gate.observe(False, 1.01, signal_allowed=True))
        self.assertFalse(gate.armed)

        self.assertFalse(gate.observe(True, 1.02, signal_allowed=False))
        self.assertTrue(gate.armed)
        self.assertEqual(gate.rearm_count, 1)

        self.assertFalse(gate.observe(False, 1.03, signal_allowed=True))
        self.assertTrue(gate.observe(False, 1.04, signal_allowed=True))
        self.assertEqual(gate.missing_started_at, 1.03)

    def test_countdown_that_never_reappears_stays_disarmed(self) -> None:
        gate = CountdownSignalGate(confirm_count=2)
        gate.disarm()
        for now in (1.0, 1.1, 1.2, 1.3):
            self.assertFalse(gate.observe(False, now, signal_allowed=True))
        self.assertFalse(gate.armed)
        self.assertEqual(gate.rearm_count, 0)


if __name__ == "__main__":
    unittest.main()
