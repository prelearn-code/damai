from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

import uiautomator2 as u2


DEFAULT_ADB = Path(r"E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe")
SKU_RESOURCE_ID = "cn.damai:id/sku_contanier"
PRICE_RESOURCE_ID = "cn.damai:id/tv_price"
SESSION_ITEM_RESOURCE_ID = "cn.damai:id/ll_perform_item"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure when a calibrated Damai price becomes actually clickable."
    )
    parser.add_argument("--serial", required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/li_ronghao_urumqi.json"),
    )
    parser.add_argument("--target-price", type=float)
    parser.add_argument("--probe-interval-ms", type=int, default=10)
    parser.add_argument(
        "--fixed-delay-ms",
        type=int,
        help="Test one fixed delay from the second-session tap to the final price coordinate.",
    )
    parser.add_argument(
        "--infer-moving-from-hit-price",
        type=float,
        help=(
            "Infer the moving target coordinate from the price hit at the final "
            "coordinate during the same fixed-delay animation."
        ),
    )
    parser.add_argument(
        "--continue-to-order-page",
        action="store_true",
        help="After the fixed-delay price tap, wait briefly, click Confirm, and stop on the order page.",
    )
    parser.add_argument("--price-to-confirm-ms", type=int, default=100)
    parser.add_argument("--stable-samples", type=int, default=3)
    parser.add_argument("--stable-tolerance-px", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=Path(
            "artifacts/wulumuqi-li-ronghao-2026-08-11-15-00-mode1/"
            "price-ready-measurement.png"
        ),
    )
    return parser.parse_args()


class PersistentAdbShell:
    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("无法创建持久 ADB shell")

    def send(self, command: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def run_marked(self, command: str, marker: str) -> None:
        assert self.process.stdout is not None
        self.send(f"{command}; echo {marker}")
        for line in self.process.stdout:
            if line.strip() == marker:
                return
        raise RuntimeError("ADB shell 在返回标记前结束")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    if args.probe_interval_ms < 5:
        raise ValueError("探测间隔不能小于 5ms")
    if args.stable_samples < 2:
        raise ValueError("稳定采样次数不能小于 2")
    if not DEFAULT_ADB.is_file():
        raise FileNotFoundError(f"ADB 不存在：{DEFAULT_ADB}")

    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    points = profile["calibrated_points"]
    target_price = float(
        args.target_price
        if args.target_price is not None
        else profile.get("target_price", 880)
    )
    price_key = f"{target_price:g}"
    price_keys = list(points["prices"])
    if price_key not in price_keys:
        raise ValueError(f"配置中没有 {price_key} 元票档")
    price_index = price_keys.index(price_key)
    configured_price_point = points["prices"][price_key]
    configured_moving_point = points.get("moving_prices", {}).get(price_key)
    purchase_point = points["purchase"]
    session_point = points["session"]
    confirm_point = points["confirm"]

    device = u2.connect(args.serial)
    purchase = device(
        resourceId="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
    )
    if not purchase.exists(timeout=0.2):
        raise RuntimeError("请先停在目标演唱会详情页，再运行测速脚本")

    shell = PersistentAdbShell(DEFAULT_ADB, args.serial)
    attempts: list[dict[str, object]] = []
    started = time.perf_counter()

    try:
        shell.run_marked(
            f"input tap {purchase_point[0]} {purchase_point[1]}",
            "__PURCHASE_DONE__",
        )
        purchase_done = time.perf_counter()

        sku_deadline = time.perf_counter() + args.timeout
        while not device(resourceId=SKU_RESOURCE_ID).exists(timeout=0):
            if time.perf_counter() >= sku_deadline:
                raise TimeoutError("票档页未出现")
            time.sleep(0.003)
        sku_ready = time.perf_counter()

        # sku_contanier is exposed before the session buttons are mounted. Do
        # not start the measurement until the second button itself is ready.
        session_item = device(
            resourceId=SESSION_ITEM_RESOURCE_ID,
            instance=int(profile.get("session_index", 1)),
        )
        session_deadline = time.perf_counter() + args.timeout
        while not session_item.exists(timeout=0):
            if time.perf_counter() >= session_deadline:
                raise TimeoutError("第二场控件未出现")
            time.sleep(0.003)
        session_control_ready = time.perf_counter()
        session_item_count = device(resourceId=SESSION_ITEM_RESOURCE_ID).count
        time.sleep(0.02)

        shell.run_marked(
            f"input tap {session_point[0]} {session_point[1]}",
            "__SESSION_DONE__",
        )
        session_done = time.perf_counter()

        interval_seconds = args.probe_interval_ms / 1000
        target = device(resourceId=PRICE_RESOURCE_ID, text=price_key)
        if args.fixed_delay_ms is not None:
            if args.fixed_delay_ms < 0:
                raise ValueError("固定点击间隔不能为负数")
            fixed_tap_point = list(
                configured_moving_point or configured_price_point
            )
            coordinate_source = (
                "profile_moving_price"
                if configured_moving_point is not None
                else "profile_final_price"
            )
            inferred_offset_y = 0
            inferred_from_price: float | None = None
            if args.infer_moving_from_hit_price is not None:
                inferred_from_price = float(args.infer_moving_from_hit_price)
                hit_price_key = f"{inferred_from_price:g}"
                if hit_price_key not in points["prices"]:
                    raise ValueError(f"配置中没有 {hit_price_key} 元票档坐标")
                hit_final_point = points["prices"][hit_price_key]
                # At the sampled animation time, the target's final Y hit the
                # wrong price. Treat the price grid as one translating surface:
                # offset = tapY - wrongPriceFinalY, movingTargetY = targetFinalY + offset.
                inferred_offset_y = int(configured_price_point[1]) - int(
                    hit_final_point[1]
                )
                fixed_tap_point[1] = int(configured_price_point[1]) + inferred_offset_y
                coordinate_source = "inferred_from_hit_price"
            target_tap_time = session_done + args.fixed_delay_ms / 1000
            remaining = target_tap_time - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            tap_started = time.perf_counter()
            if args.continue_to_order_page:
                if args.price_to_confirm_ms < 0:
                    raise ValueError("票价到确认的间隔不能为负数")
                confirm_delay = args.price_to_confirm_ms / 1000
                shell.run_marked(
                    f"input tap {fixed_tap_point[0]} {fixed_tap_point[1]}; "
                    f"sleep {confirm_delay:g}; "
                    f"input tap {confirm_point[0]} {confirm_point[1]}",
                    "__PRICE_AND_CONFIRM_DONE__",
                )
            else:
                shell.run_marked(
                    f"input tap {fixed_tap_point[0]} {fixed_tap_point[1]}",
                    "__FIXED_DELAY_PRICE_TAP_DONE__",
                )
            tap_done = time.perf_counter()

            if args.continue_to_order_page:
                order_title = device(resourceId="cn.damai:id/order_activity_title")
                order_deadline = time.perf_counter() + args.timeout
                while not order_title.exists(timeout=0):
                    if time.perf_counter() >= order_deadline:
                        raise TimeoutError("点击票价和确认后未进入确认购买页")
                    time.sleep(interval_seconds)
                order_page_at = time.perf_counter()
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                device.screenshot(str(args.screenshot))
                print(
                    json.dumps(
                        {
                            "status": "stopped_on_order_page_before_submit",
                            "target_price": target_price,
                            "configured_session_to_price_ms": args.fixed_delay_ms,
                            "configured_price_to_confirm_ms": args.price_to_confirm_ms,
                            "price_and_confirm_done_ms_after_session": round(
                                (tap_done - session_done) * 1000, 1
                            ),
                            "order_page_ms_after_session": round(
                                (order_page_at - session_done) * 1000, 1
                            ),
                            "tap_point": fixed_tap_point,
                            "coordinate_source": coordinate_source,
                            "screenshot": str(args.screenshot.resolve()),
                            "safety": "immediate_submit_not_clicked; alipay_not_opened",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return

            selected = False
            verify_deadline = time.perf_counter() + 1
            while time.perf_counter() < verify_deadline:
                if target.exists(timeout=0):
                    selected = True
                    break
                time.sleep(interval_seconds)
            verified_at = time.perf_counter()
            any_price = device(resourceId=PRICE_RESOURCE_ID)
            observed_price = (
                any_price.get_text() if any_price.exists(timeout=0) else None
            )
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            device.screenshot(str(args.screenshot))
            print(
                json.dumps(
                    {
                        "status": (
                            "fixed_delay_selected_target"
                            if selected
                            else "fixed_delay_missed_target"
                        ),
                        "target_price": target_price,
                        "configured_delay_ms": args.fixed_delay_ms,
                        "tap_started_ms_after_session": round(
                            (tap_started - session_done) * 1000, 1
                        ),
                        "tap_done_ms_after_session": round(
                            (tap_done - session_done) * 1000, 1
                        ),
                        "verified_ms_after_session": round(
                            (verified_at - session_done) * 1000, 1
                        ),
                        "tap_point": fixed_tap_point,
                        "coordinate_source": coordinate_source,
                        "inferred_from_hit_price": inferred_from_price,
                        "inferred_vertical_offset_px": inferred_offset_y,
                        "observed_price": observed_price,
                        "screenshot": str(args.screenshot.resolve()),
                        "safety": "confirm_not_clicked; order_not_created",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        # ll_perform_item is ordered as all session items followed by all price
        # items. Track the target node until its bounds stop moving; clicking a
        # fixed final coordinate during the entrance animation was observed to
        # select 500 instead of 880.
        target_item = device(
            resourceId=SESSION_ITEM_RESOURCE_ID,
            instance=session_item_count + price_index,
        )
        probe_durations: list[float] = []
        bounds_samples: list[dict[str, object]] = []
        deadline = session_done + args.timeout
        selected_at: float | None = None
        target_node_ready_at: float | None = None
        target_bounds_stable_at: float | None = None
        stable_count = 0
        previous_center: tuple[int, int] | None = None
        stable_point: tuple[int, int] | None = None
        while time.perf_counter() < deadline:
            node_probe_started = time.perf_counter()
            if not target_item.exists(timeout=0):
                probe_durations.append(time.perf_counter() - node_probe_started)
                time.sleep(interval_seconds)
                continue
            if target_node_ready_at is None:
                target_node_ready_at = time.perf_counter()
            try:
                bounds = target_item.info["bounds"]
            except Exception:
                time.sleep(interval_seconds)
                continue
            x = (int(bounds["left"]) + int(bounds["right"])) // 2
            y = (int(bounds["top"]) + int(bounds["bottom"])) // 2
            sampled_at = time.perf_counter()
            bounds_samples.append(
                {
                    "milliseconds_after_session": round(
                        (sampled_at - session_done) * 1000, 1
                    ),
                    "center": [x, y],
                    "bounds": [
                        int(bounds["left"]),
                        int(bounds["top"]),
                        int(bounds["right"]),
                        int(bounds["bottom"]),
                    ],
                }
            )
            if previous_center is not None and (
                abs(x - previous_center[0]) <= args.stable_tolerance_px
                and abs(y - previous_center[1]) <= args.stable_tolerance_px
            ):
                stable_count += 1
            else:
                stable_count = 1
            previous_center = (x, y)
            if stable_count >= args.stable_samples:
                stable_point = (x, y)
                target_bounds_stable_at = sampled_at
                break
            time.sleep(interval_seconds)
        if stable_point is None or target_bounds_stable_at is None:
            raise TimeoutError(f"{price_key} 元控件在 {args.timeout:g}s 内仍未停止移动")

        tap_started = time.perf_counter()
        shell.run_marked(
            f"input tap {stable_point[0]} {stable_point[1]}",
            "__STABLE_PRICE_TAP_DONE__",
        )
        tap_done = time.perf_counter()
        attempts.append(
            {
                "number": 1,
                "point": list(stable_point),
                "tap_started_ms": round((tap_started - session_done) * 1000, 1),
                "tap_done_ms": round((tap_done - session_done) * 1000, 1),
            }
        )

        selected_deadline = time.perf_counter() + 1
        while time.perf_counter() < selected_deadline:
            probe_started = time.perf_counter()
            selected = target.exists(timeout=0)
            probe_durations.append(time.perf_counter() - probe_started)
            if selected:
                selected_at = time.perf_counter()
                break
            time.sleep(interval_seconds)
        if selected_at is None:
            raise RuntimeError(
                f"{price_key} 元控件已停止移动并点击，但页面未显示选中 {price_key} 元"
            )

        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        device.screenshot(str(args.screenshot))
        last_attempt = attempts[-1] if attempts else None
        probe_ms = statistics.median(probe_durations) * 1000
        result = {
            "status": "target_price_became_clickable",
            "target_price": target_price,
            "milliseconds_after_session": round(
                (selected_at - session_done) * 1000, 1
            ),
            "target_node_ready_ms_after_session": (
                round((target_node_ready_at - session_done) * 1000, 1)
                if target_node_ready_at is not None
                else None
            ),
            "target_bounds_stable_ms_after_session": round(
                (target_bounds_stable_at - session_done) * 1000, 1
            ),
            "successful_tap_attempt": last_attempt,
            "attempt_count": len(attempts),
            "bounds_samples": bounds_samples,
            "stable_rule": {
                "consecutive_samples": args.stable_samples,
                "tolerance_px": args.stable_tolerance_px,
            },
            "median_selector_probe_ms": round(probe_ms, 1),
            "timings_ms": {
                "detail_to_purchase_tap_done": round(
                    (purchase_done - started) * 1000, 1
                ),
                "detail_to_sku_ready": round((sku_ready - started) * 1000, 1),
                "detail_to_session_control_ready": round(
                    (session_control_ready - started) * 1000, 1
                ),
                "detail_to_session_tap_done": round(
                    (session_done - started) * 1000, 1
                ),
                "detail_to_880_selected": round((selected_at - started) * 1000, 1),
            },
            "probe_interval_requested_ms": args.probe_interval_ms,
            "screenshot": str(args.screenshot.resolve()),
            "safety": "stopped_after_880_selection; confirm_not_clicked",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        shell.close()


if __name__ == "__main__":
    main()
