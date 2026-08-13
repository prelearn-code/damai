from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from damai_mode0 import (  # noqa: E402
    ALIPAY_PACKAGE,
    CLICK_INTERVAL_S,
    DAMAI_PACKAGE,
    FastPageProbe,
    FIRST_STATE_CHECK_AFTER_S,
    MAIN_ACTION_POINT,
    MAX_RUNTIME_AFTER_SALE_S,
    PRECLICK_LEAD_S,
    RETRY_DIALOG_POINT,
    STATE_CHECK_INTERVAL_S,
    classify_fast_state,
    make_schedule,
    parse_sale_time,
)


class Mode0LogicTests(unittest.TestCase):
    def test_timing_constants_match_requested_values(self) -> None:
        self.assertEqual(PRECLICK_LEAD_S, 2.0)
        self.assertEqual(CLICK_INTERVAL_S, 0.150)
        self.assertEqual(FIRST_STATE_CHECK_AFTER_S, 2.0)
        self.assertEqual(STATE_CHECK_INTERVAL_S, 1.0)
        self.assertEqual(MAX_RUNTIME_AFTER_SALE_S, 600.0)

    def test_schedule_is_anchored_to_supplied_sale_time(self) -> None:
        schedule = make_schedule(
            datetime(2026, 8, 18, 13, 0, 0),
            datetime(2026, 8, 18, 12, 59, 50),
            100.0,
        )
        self.assertEqual(schedule.sale_monotonic, 110.0)
        self.assertEqual(schedule.click_start_monotonic, 108.0)
        self.assertEqual(schedule.first_probe_monotonic, 112.0)
        self.assertEqual(schedule.deadline_monotonic, 710.0)

    def test_points_are_inside_calibrated_screen(self) -> None:
        for point in (MAIN_ACTION_POINT, RETRY_DIALOG_POINT):
            self.assertGreaterEqual(point[0], 0)
            self.assertLess(point[0], 1440)
            self.assertGreaterEqual(point[1], 0)
            self.assertLess(point[1], 3200)

    def test_classifies_order_page(self) -> None:
        self.assertEqual(
            classify_fast_state("cn.damai", order_page_visible=True),
            "order_page",
        )

    def test_fast_probe_queries_only_the_order_page_node(self) -> None:
        class Selector:
            def exists(self, timeout: float) -> bool:
                self.timeout = timeout
                return True

        class Device:
            def __init__(self) -> None:
                self.selector_calls: list[dict[str, object]] = []
                self.selector = Selector()

            def __call__(self, **kwargs: object) -> Selector:
                self.selector_calls.append(kwargs)
                return self.selector

            def app_current(self) -> dict[str, str]:
                return {"package": DAMAI_PACKAGE}

        device = Device()
        probe = FastPageProbe(device)

        self.assertEqual(probe.read(), (DAMAI_PACKAGE, "order_page"))
        self.assertEqual(
            device.selector_calls,
            [{"resourceId": "cn.damai:id/order_activity_title"}],
        )
        self.assertEqual(device.selector.timeout, 0)

    def test_classifies_alipay_first(self) -> None:
        self.assertEqual(classify_fast_state(ALIPAY_PACKAGE), "alipay")

    def test_parses_supplied_time(self) -> None:
        self.assertEqual(
            parse_sale_time("2026-08-18 13:00:00"),
            datetime(2026, 8, 18, 13, 0, 0),
        )


if __name__ == "__main__":
    unittest.main()
