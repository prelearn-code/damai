from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable

from PIL import Image
import uiautomator2 as u2


DAMAI_PACKAGE = "cn.damai"
ALIPAY_PACKAGE = "com.eg.android.AlipayGphone"
DEFAULT_ADB = Path(r"E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe")

PURCHASE_RESOURCE = "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
SKU_RESOURCE = "cn.damai:id/sku_contanier"
SESSION_ITEM_RESOURCE = "cn.damai:id/ll_perform_item"
PRICE_LAYOUT_RESOURCE = "cn.damai:id/project_detail_perform_price_flowlayout"
CONFIRM_RESOURCE = "cn.damai:id/btn_buy_view"
ORDER_TITLE_RESOURCE = "cn.damai:id/order_activity_title"
RETRY_RESOURCE = "cn.damai:id/damai_theme_dialog_confirm_btn"
SALE_TIME_RESOURCE = "cn.damai:id/id_project_count_sell_time"
PURCHASE_RETRY_INTERVAL_S = 0.1

SALE_TIME_PATTERN = re.compile(
    r"^\s*(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\s*开抢\s*$"
)

MODE_NAMES = {
    1: "reserved_price_available",
    2: "reserved_price_reselect",
    3: "unreserved_select_session_and_price",
}


def parse_page_sale_time(text: str) -> tuple[int, int, int, int]:
    match = SALE_TIME_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"无法识别页面开售时间：{text!r}")
    month, day, hour, minute = (int(value) for value in match.groups())
    try:
        datetime(2000, month, day, hour, minute)
    except ValueError as exc:
        raise ValueError(f"页面开售时间无效：{text!r}") from exc
    return month, day, hour, minute


def validate_page_sale_time(text: str, expected: datetime) -> None:
    actual = parse_page_sale_time(text)
    wanted = (expected.month, expected.day, expected.hour, expected.minute)
    if actual != wanted:
        actual_text = f"{actual[0]:02d}-{actual[1]:02d} {actual[2]:02d}:{actual[3]:02d}"
        wanted_text = (
            f"{wanted[0]:02d}-{wanted[1]:02d} {wanted[2]:02d}:{wanted[3]:02d}"
        )
        raise ValueError(
            f"页面开售时间为 {actual_text}，与 --sale-time 的 {wanted_text} 不一致"
        )


def seconds_until_timer(seconds_to_sale: float, fallback_delay: float) -> float:
    """Return zero when the sale-time fallback deadline has already passed."""
    return max(0.0, seconds_to_sale + fallback_delay)


@dataclass
class CountdownSignalGate:
    confirm_count: int
    armed: bool = True
    false_streak: int = 0
    rearm_count: int = 0
    missing_started_at: float | None = None

    def disarm(self) -> None:
        self.armed = False
        self.false_streak = 0
        self.missing_started_at = None

    def observe(self, present: bool, now: float, signal_allowed: bool) -> bool:
        if present:
            if not self.armed:
                self.armed = True
                self.rearm_count += 1
            self.false_streak = 0
            self.missing_started_at = None
            return False
        if not self.armed or not signal_allowed:
            return False
        if self.false_streak == 0:
            self.missing_started_at = now
        self.false_streak += 1
        return self.false_streak >= self.confirm_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Damai detail-page-to-Alipay checkout; never clicks Alipay Pay."
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--selection-mode", type=int, choices=(1, 2, 3))
    parser.add_argument("--target-price", type=float)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--queue-timeout", type=float)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--wait-for-sale",
        action="store_true",
        help="Wait on the detail page, refreshing until the purchase entry is actionable.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Use selector-based countdown detection with --wait-for-sale; "
            "the configured sale time provides a bounded timer fallback."
        ),
    )
    parser.add_argument(
        "--sale-time",
        help=(
            "Local sale time, required with --fast; "
            "for example: 2026-08-15 15:00:00."
        ),
    )
    parser.add_argument("--sale-wait-timeout", type=float)
    parser.add_argument("--refresh-interval", type=float)
    parser.add_argument(
        "--stop-before-submit",
        action="store_true",
        help="Stop on Damai's order page without clicking Immediate Submit.",
    )
    # Compatibility flags accepted by earlier commands. The refactored script
    # is always the calibrated fast path and submits unless --stop-before-submit.
    parser.add_argument("--ultra-fast", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--commit-order", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-duplicate-order", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.fast and not args.wait_for_sale:
        parser.error("--fast 需要与 --wait-for-sale 一起使用")
    if args.fast and not args.sale_time:
        parser.error("--fast 必须提供 --sale-time 作为定时器兜底")

    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    args.profile_data = profile
    if args.selection_mode is None:
        args.selection_mode = int(profile.get("selection_mode", 1))
    if args.target_price is None:
        args.target_price = float(profile.get("target_price", 880))
    if args.timeout is None:
        args.timeout = float(profile.get("timeout", 8))
    if args.queue_timeout is None:
        args.queue_timeout = float(profile.get("queue_timeout", 60))
    if args.artifacts is None:
        args.artifacts = Path(profile.get("artifacts", "artifacts/checkout"))
    sale_wait = profile.get("sale_wait", {})
    if args.sale_wait_timeout is None:
        args.sale_wait_timeout = float(sale_wait.get("timeout", 1800))
    if args.refresh_interval is None:
        args.refresh_interval = float(sale_wait.get("refresh_interval", 0.5))
    args.sale_time_value = (
        datetime.fromisoformat(args.sale_time) if args.sale_time else None
    )
    if args.sale_time_value is not None and args.sale_time_value.tzinfo is not None:
        # All runtime comparisons use the computer's local wall clock.
        args.sale_time_value = args.sale_time_value.astimezone().replace(tzinfo=None)
    return args


class CheckoutFlow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.profile = args.profile_data
        self.points: dict[str, object] = self.profile["calibrated_points"]
        self.delays: dict[str, float] = self.points.get("delays", {})
        self.target_price = float(args.target_price)
        self.price_key = f"{self.target_price:g}"
        self.adb_path = Path(self.profile.get("adb_path", DEFAULT_ADB))
        if not self.adb_path.is_file():
            raise FileNotFoundError(f"ADB 不存在：{self.adb_path}")
        self.validate_configuration()
        self.device = u2.connect(args.serial)
        expected_size = tuple(self.profile.get("screen_size", [1440, 3200]))
        actual_size = tuple(self.device.window_size())
        if actual_size != expected_size:
            raise RuntimeError(
                f"手机分辨率为 {actual_size[0]}×{actual_size[1]}，"
                f"但坐标按 {expected_size[0]}×{expected_size[1]} 校准"
            )
        args.artifacts.mkdir(parents=True, exist_ok=True)
        self.shell = subprocess.Popen(
            [str(self.adb_path), "-s", args.serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.shell.stdin is None or self.shell.stdout is None:
            raise RuntimeError("无法创建持久 ADB shell")
        self.shell_marker = 0
        self.started = 0.0
        self.timings: dict[str, float] = {}
        self.event_times: dict[str, float] = {}
        self.refresh_count = 0
        self.sale_ready_mask: set[int] | None = None
        self.sale_trigger: str | None = None
        self.sale_to_purchase_dispatch_ms: float | None = None
        self.sale_to_purchase_complete_ms: float | None = None
        self.countdown_rearm_count = 0
        self.countdown_last_seen_to_sale_ms: float | None = None
        self.countdown_missing_duration_ms: float | None = None
        self.last_refresh_to_sale_ms: float | None = None
        self.countdown_armed_at_trigger: bool | None = None
        self.submit_attempts = 0
        self.retry_attempts = 0

    @staticmethod
    def valid_point(value: object) -> bool:
        return (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(item, (int, float)) for item in value)
        )

    def require_point(self, group: str, price_key: str | None = None) -> None:
        if group not in self.points:
            raise ValueError(f"配置缺少坐标组：{group}")
        value = self.points[group]
        label = group
        if price_key is not None:
            if not isinstance(value, dict) or price_key not in value:
                raise ValueError(f"配置缺少 {group}.{price_key} 坐标")
            value = value[price_key]
            label = f"{group}.{price_key}"
        if not self.valid_point(value):
            raise ValueError(f"无效坐标：{label}={value!r}")

    def validate_configuration(self) -> None:
        if self.args.selection_mode not in MODE_NAMES:
            raise ValueError("selection_mode 只能是 1、2 或 3")
        if self.target_price <= 0:
            raise ValueError("target_price 必须大于 0")
        self.require_point("purchase")
        for name, value in self.delays.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"延迟必须是非负数：{name}={value!r}")
        if self.args.selection_mode == 2:
            self.require_point("confirm")
            if not any(
                isinstance(self.points.get(group), dict)
                and self.price_key in self.points[group]
                and self.valid_point(self.points[group][self.price_key])
                for group in ("replacement_prices", "expanded_prices", "prices")
            ):
                raise ValueError(f"模式 2 缺少 {self.price_key} 元替代票价坐标")
        if self.args.selection_mode == 3:
            self.require_point("confirm")
            self.require_point("session")
            self.require_point("moving_prices", self.price_key)

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.event_times[name] = now
        self.timings[name] = round(now - self.started, 3)

    def wait_for(
        self,
        predicate: Callable[[], bool],
        description: str,
        timeout: float | None = None,
    ) -> None:
        deadline = time.perf_counter() + (timeout or self.args.timeout)
        while time.perf_counter() < deadline:
            if predicate():
                return
            time.sleep(0.003)
        raise TimeoutError(f"等待超时：{description}")

    def adb_taps(
        self,
        *tap_points: tuple[int, int],
        initial_delay: float = 0.0,
        between_delay: float | tuple[float, ...] = 0.0,
    ) -> None:
        commands: list[str] = []
        if initial_delay > 0:
            commands.append(f"sleep {initial_delay:g}")
        for index, (x, y) in enumerate(tap_points):
            if index:
                delay = (
                    between_delay[index - 1]
                    if isinstance(between_delay, tuple)
                    else between_delay
                )
                if delay > 0:
                    commands.append(f"sleep {delay:g}")
            commands.append(f"input tap {int(x)} {int(y)}")
        if self.shell.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.shell.stdin is not None
        assert self.shell.stdout is not None
        self.shell_marker += 1
        marker = f"__DAMAI_TAP_{self.shell_marker}_DONE__"
        self.shell.stdin.write("; ".join(commands) + f"; echo {marker}\n")
        self.shell.stdin.flush()
        deadline = time.perf_counter() + 4
        while time.perf_counter() < deadline:
            line = self.shell.stdout.readline()
            if not line:
                raise RuntimeError("持久 ADB shell 在点击完成前结束")
            if line.strip() == marker:
                return
        raise TimeoutError("持久 ADB shell 点击执行超时")

    def adb_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> None:
        if self.shell.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.shell.stdin is not None
        assert self.shell.stdout is not None
        self.shell_marker += 1
        marker = f"__DAMAI_SWIPE_{self.shell_marker}_DONE__"
        command = (
            f"input swipe {int(start[0])} {int(start[1])} "
            f"{int(end[0])} {int(end[1])} {int(duration_ms)}"
        )
        self.shell.stdin.write(f"{command}; echo {marker}\n")
        self.shell.stdin.flush()
        for line in self.shell.stdout:
            if line.strip() == marker:
                return
        raise RuntimeError("持久 ADB shell 在刷新完成前结束")

    def close(self) -> None:
        if self.shell.poll() is None:
            self.shell.terminate()
            try:
                self.shell.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.shell.kill()

    def point(self, group: str, price_key: str | None = None) -> tuple[int, int]:
        value = self.points[group]
        if price_key is not None:
            value = value[price_key]
        return int(value[0]), int(value[1])

    def sale_ready_selector(self) -> object:
        if self.args.selection_mode == 1:
            return self.device(resourceId=CONFIRM_RESOURCE, clickable=True)
        if self.args.selection_mode == 2:
            return self.device(resourceId=PRICE_LAYOUT_RESOURCE)
        return self.device(resourceId=SKU_RESOURCE)

    @staticmethod
    def white_text_mask(image: Image.Image) -> set[int]:
        """Return near-white pixels used by the fixed bottom-button label."""
        pixels = image.convert("RGB").get_flattened_data()
        return {
            index
            for index, (red, green, blue) in enumerate(pixels)
            if min(red, green, blue) > 215
            and max(red, green, blue) - min(red, green, blue) < 32
        }

    def sale_button_similarity(self, sale_wait: dict[str, object]) -> float:
        roi_value = sale_wait.get("button_text_roi", [620, 3000, 1290, 3165])
        roi = tuple(int(value) for value in roi_value)
        if len(roi) != 4:
            raise ValueError("sale_wait.button_text_roi 必须包含 4 个整数")

        if self.sale_ready_mask is None:
            reference_path = Path(
                str(
                    sale_wait.get(
                        "ready_reference",
                        "artifacts/li-ronghao-1080-retest/01-detail.png",
                    )
                )
            )
            if not reference_path.is_file():
                raise FileNotFoundError(f"开售按钮参考图不存在：{reference_path}")
            with Image.open(reference_path) as reference:
                self.sale_ready_mask = self.white_text_mask(reference.crop(roi))

        current = self.device.screenshot().crop(roi)
        current_mask = self.white_text_mask(current)
        union = current_mask | self.sale_ready_mask
        if not union:
            return 0.0
        return len(current_mask & self.sale_ready_mask) / len(union)

    def wait_for_sale(self) -> str:
        """Refresh the detail page until the purchase entry actually opens."""
        purchase = self.device(resourceId=PURCHASE_RESOURCE)
        if not purchase.exists(timeout=0.2):
            raise RuntimeError("请先停在目标演唱会详情页")

        sale_wait = self.profile.get("sale_wait", {})
        pre_poll_seconds = float(sale_wait.get("pre_poll_seconds", 5.0))
        refresh_settle = float(sale_wait.get("refresh_settle", 0.25))
        probe_timeout = float(sale_wait.get("purchase_probe_timeout", 0.3))
        refresh_start_value = sale_wait.get("refresh_start", [720, 500])
        refresh_end_value = sale_wait.get("refresh_end", [720, 1800])
        refresh_start = (int(refresh_start_value[0]), int(refresh_start_value[1]))
        refresh_end = (int(refresh_end_value[0]), int(refresh_end_value[1]))
        refresh_duration_ms = int(sale_wait.get("refresh_duration_ms", 180))
        ready_threshold = float(sale_wait.get("ready_similarity_threshold", 0.55))

        deadline = time.perf_counter() + self.args.sale_wait_timeout
        ready = self.sale_ready_selector()
        while time.perf_counter() < deadline:
            if self.args.sale_time_value is not None:
                seconds_to_sale = (
                    self.args.sale_time_value - datetime.now()
                ).total_seconds()
                if seconds_to_sale > pre_poll_seconds:
                    time.sleep(min(1.0, seconds_to_sale - pre_poll_seconds))
                    continue

            # The visible label is canvas-rendered and absent from the UI tree.
            # Match only its fixed bottom-button text region against a known
            # on-sale reference, then make the successful detection's first tap
            # the real purchase tap.
            similarity = self.sale_button_similarity(sale_wait)
            if similarity >= ready_threshold:
                self.mark("sale_opened")
                self.adb_taps(self.point("purchase"))
                self.mark("detail_clicked")
                if ready.exists(timeout=probe_timeout):
                    self.mark("ticket_panel")
                    return "sku"
                raise TimeoutError(
                    "已识别到立即预订，但点击后票档页未就绪"
                )

            self.adb_swipe(refresh_start, refresh_end, refresh_duration_ms)
            self.refresh_count += 1
            time.sleep(max(refresh_settle, self.args.refresh_interval))

        raise TimeoutError(
            f"等待开售超时：已刷新 {self.refresh_count} 次"
        )

    def wait_for_sale_fast(self) -> str:
        """Wait for the countdown node to disappear, with debounce and timer fallback."""
        purchase = self.device(resourceId=PURCHASE_RESOURCE)
        if not purchase.exists(timeout=0.2):
            raise RuntimeError("请先停在目标演唱会详情页")
        if self.args.sale_time_value is None:
            raise ValueError("--fast 必须提供 --sale-time 作为定时器兜底")

        page_sale_time = self.device(resourceId=SALE_TIME_RESOURCE)
        if not page_sale_time.exists(timeout=min(1.0, self.args.timeout)):
            raise RuntimeError("未检测到页面开售时间，请确认停在预约倒计时详情页")
        page_sale_time_text = str(page_sale_time.get_text() or "").strip()
        validate_page_sale_time(page_sale_time_text, self.args.sale_time_value)

        sale_wait = self.profile.get("sale_wait", {})
        selector = sale_wait.get("selector", {})
        if not isinstance(selector, dict):
            raise ValueError("sale_wait.selector 必须是 JSON 对象")
        resource_id = str(
            selector.get("resource_id", "cn.damai:id/id_project_count_down_layout")
        )
        confirm_count = max(1, min(5, int(selector.get("confirm_count", 2))))
        poll_interval = max(0.01, float(selector.get("poll_interval_s", 0.06)))
        refresh_interval = max(0.1, float(selector.get("refresh_interval_s", 10.0)))
        refresh_stop_at = max(0.0, float(selector.get("refresh_stop_at_s", 2.0)))
        fallback_delay = max(0, int(selector.get("t_fallback_ms", 300))) / 1000.0
        # Ignore transient selector misses well before the known sale time.
        # The countdown view can briefly unmount during its own redraw.
        signal_accept_before = 0.0
        refresh_settle = max(0.0, float(sale_wait.get("refresh_settle", 0.25)))
        refresh_start_value = sale_wait.get("refresh_start", [720, 500])
        refresh_end_value = sale_wait.get("refresh_end", [720, 1800])
        refresh_start = (int(refresh_start_value[0]), int(refresh_start_value[1]))
        refresh_end = (int(refresh_end_value[0]), int(refresh_end_value[1]))
        refresh_duration_ms = int(sale_wait.get("refresh_duration_ms", 180))

        count_down = self.device(resourceId=resource_id)

        # Establish the not-yet-on-sale baseline first. Without this transition,
        # an incorrect or half-rendered page would look exactly like "sale open".
        baseline_hits = 0
        baseline_deadline = time.perf_counter() + min(3.0, self.args.timeout)
        while time.perf_counter() < baseline_deadline:
            if count_down.exists(timeout=0):
                self.countdown_last_seen_to_sale_ms = round(
                    (
                        self.args.sale_time_value - datetime.now()
                    ).total_seconds()
                    * 1000,
                    3,
                )
                baseline_hits += 1
                if baseline_hits >= 3:
                    break
            else:
                baseline_hits = 0
            time.sleep(poll_interval)
        else:
            raise RuntimeError("未检测到倒计时区域（可能已开售或不在倒计时页）")

        seconds_to_sale = (
            self.args.sale_time_value - datetime.now()
        ).total_seconds()
        fallback_at = time.perf_counter() + seconds_until_timer(
            seconds_to_sale,
            fallback_delay,
        )

        gate = CountdownSignalGate(confirm_count=confirm_count)
        trigger: str | None = None
        last_refresh = time.perf_counter()
        refresh_cooldown_until = 0.0
        deadline = time.perf_counter() + self.args.sale_wait_timeout
        while time.perf_counter() < deadline:
            now = time.perf_counter()
            seconds_to_sale = (
                self.args.sale_time_value - datetime.now()
            ).total_seconds()
            # The timer is intentionally authoritative. At its deadline, click
            # immediately without waiting for another selector probe.
            if now >= fallback_at:
                trigger = "timer"
                break

            signal_allowed = (
                seconds_to_sale <= signal_accept_before
            )
            should_probe = (
                now >= refresh_cooldown_until
                and (not gate.armed or signal_allowed)
            )
            if should_probe:
                count_down_present = count_down.exists(timeout=0)
                # A selector IPC call may straddle the timer deadline. The
                # timer still wins and no additional signal confirmation runs.
                if time.perf_counter() >= fallback_at:
                    trigger = "timer"
                    break
                if count_down_present:
                    self.countdown_last_seen_to_sale_ms = round(
                        seconds_to_sale * 1000,
                        3,
                    )
                if gate.observe(count_down_present, now, signal_allowed):
                    trigger = "signal"
                    break

            should_refresh = now - last_refresh >= refresh_interval
            should_refresh = should_refresh and seconds_to_sale > refresh_stop_at
            if should_refresh and now >= refresh_cooldown_until:
                self.adb_swipe(refresh_start, refresh_end, refresh_duration_ms)
                self.refresh_count += 1
                gate.disarm()
                # A pull-to-refresh temporarily unmounts the node; exclude that window.
                refresh_cooldown_until = time.perf_counter() + refresh_settle
                last_refresh = time.perf_counter()
                self.last_refresh_to_sale_ms = round(
                    (
                        self.args.sale_time_value - datetime.now()
                    ).total_seconds()
                    * 1000,
                    3,
                )

            time.sleep(poll_interval)
        else:
            raise TimeoutError(f"等待开售超时：已刷新 {self.refresh_count} 次")

        trigger_at = time.perf_counter()
        self.sale_trigger = trigger
        self.countdown_rearm_count = gate.rearm_count
        self.countdown_armed_at_trigger = gate.armed
        if gate.missing_started_at is not None:
            self.countdown_missing_duration_ms = round(
                (trigger_at - gate.missing_started_at) * 1000,
                3,
            )
        self.mark("sale_opened")
        if self.args.sale_time_value is not None:
            self.sale_to_purchase_dispatch_ms = round(
                (datetime.now() - self.args.sale_time_value).total_seconds() * 1000,
                3,
            )
        self.adb_taps(self.point("purchase"))
        if self.args.sale_time_value is not None:
            self.sale_to_purchase_complete_ms = round(
                (datetime.now() - self.args.sale_time_value).total_seconds() * 1000,
                3,
            )
        self.mark("detail_clicked")
        ready = self.sale_ready_selector()
        ready_deadline = time.perf_counter() + self.args.timeout
        next_purchase_retry = time.perf_counter() + PURCHASE_RETRY_INTERVAL_S
        while time.perf_counter() < ready_deadline:
            if ready.exists(timeout=0):
                self.mark("ticket_panel")
                return "sku"
            now = time.perf_counter()
            if now >= next_purchase_retry and purchase.exists(timeout=0):
                self.adb_taps(self.point("purchase"))
                next_purchase_retry = (
                    time.perf_counter() + PURCHASE_RETRY_INTERVAL_S
                )
            time.sleep(0.003)
        raise TimeoutError(
            f"开售触发后票档页未就绪（trigger={self.sale_trigger}）"
        )

    def open_ticket_panel(self) -> str:
        purchase = self.device(resourceId=PURCHASE_RESOURCE)
        if not purchase.exists(timeout=0.15):
            raise RuntimeError("请先停在目标演唱会详情页")
        self.adb_taps(self.point("purchase"))
        self.mark("detail_clicked")

        # In mode 1 the reserved session and price are already selected. The
        # Confirm button is the only readiness signal needed before continuing.
        if self.args.selection_mode == 1:
            self.wait_for_confirm_button()
            self.mark("ticket_panel")
            return "sku"

        # In mode 2 the replacement price grid and Confirm button mount
        # together. Use the price layout as the single readiness signal and
        # avoid the generic SKU/order checks on this latency-critical path.
        if self.args.selection_mode == 2:
            self.wait_for(
                lambda: self.device(resourceId=PRICE_LAYOUT_RESOURCE).exists(
                    timeout=0
                ),
                "预约场次的替代票价列表",
                timeout=2,
            )
            self.mark("ticket_panel")
            return "sku"

        self.wait_for(
            lambda: self.device(resourceId=SKU_RESOURCE).exists(timeout=0)
            or self.device(resourceId=ORDER_TITLE_RESOURCE).exists(timeout=0),
            "票档页或确认购买页",
        )
        if self.device(resourceId=ORDER_TITLE_RESOURCE).exists(timeout=0):
            self.mark("order_page_direct")
            self.mark("order_page")
            return "order"
        self.mark("ticket_panel")
        return "sku"

    def wait_for_confirm_button(self) -> None:
        confirm = self.device(resourceId=CONFIRM_RESOURCE, clickable=True)
        self.wait_for(
            lambda: confirm.exists(timeout=0),
            "可点击的票档确认按钮",
        )

    def select_mode_1(self) -> None:
        """Reservation exists and its saved price is still available."""
        self.device(resourceId=CONFIRM_RESOURCE, clickable=True).click()
        self.mark("confirm_clicked")

    def replacement_price_point(self) -> tuple[int, int]:
        for group in ("replacement_prices", "expanded_prices", "prices"):
            values = self.points.get(group, {})
            if self.price_key in values:
                return int(values[self.price_key][0]), int(values[self.price_key][1])
        raise ValueError(f"模式 2 没有 {self.price_key} 元替代票价坐标")

    def select_mode_2(self) -> None:
        """Reservation exists, but its saved price sold out; choose replacement."""
        # The container and Confirm button may be exposed before the replacement
        # price grid has finished its entrance layout. Treat price_layout_settle
        # as a minimum detail-click-to-price-tap time instead of blindly sleeping
        # the full value after the UI probes, so time already spent waiting for
        # the controls counts toward stabilization.
        detail_clicked_at = self.event_times.get("detail_clicked")
        if detail_clicked_at is None:
            raise RuntimeError("缺少购票入口点击时间")
        earliest_price_tap = detail_clicked_at + float(
            self.delays.get("price_layout_settle", 0.0)
        )
        remaining = earliest_price_tap - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)

        self.adb_taps(
            self.replacement_price_point(),
            self.point("confirm"),
            between_delay=float(self.delays.get("replacement_price_to_confirm", 0.1)),
        )
        self.mark("replacement_price_and_confirm_clicked")

    def select_mode_3(self) -> None:
        """No reservation; select the configured session and moving price."""
        session_index = int(self.profile.get("session_index", 1))
        session_item = self.device(
            resourceId=SESSION_ITEM_RESOURCE, instance=session_index
        )
        self.wait_for(
            lambda: session_item.exists(timeout=0),
            "第二场控件",
            timeout=2,
        )
        # Match the proven standalone path. This count call confirms that the
        # complete session collection has mounted before the calibrated tap.
        session_count = self.device(resourceId=SESSION_ITEM_RESOURCE).count
        if session_count <= session_index:
            raise RuntimeError(
                f"页面只有 {session_count} 个场次，无法选择第 {session_index + 1} 场"
            )
        time.sleep(float(self.delays.get("session_control_settle", 0.02)))
        self.adb_taps(self.point("session"))
        session_done = time.perf_counter()
        self.mark("session_clicked")

        session_to_price = float(
            self.delays.get("session_to_moving_price", 0.15)
        )
        remaining = session_done + session_to_price - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)
        self.adb_taps(
            self.point("moving_prices", self.price_key),
            self.point("confirm"),
            between_delay=float(self.delays.get("price_to_confirm", 0.1)),
        )
        self.mark("fixed_price_and_confirm_clicked")

    def enter_order_page(self) -> None:
        self.wait_for(
            lambda: self.device(resourceId=ORDER_TITLE_RESOURCE).exists(timeout=0),
            "确认购买页",
        )
        self.mark("order_page")

    def submit_order(self) -> None:
        submit = self.device(
            packageName=DAMAI_PACKAGE, text="立即提交", clickable=True
        )
        self.wait_for(
            lambda: submit.exists(timeout=0),
            "可点击的立即提交按钮",
        )
        submit.click()
        self.submit_attempts += 1
        self.mark("submitted")

    def payment_result(self) -> dict[str, object]:
        self.mark("payment_page")
        sale_detection_to_purchase_complete_ms = None
        if "sale_opened" in self.event_times and "detail_clicked" in self.event_times:
            sale_detection_to_purchase_complete_ms = round(
                (
                    self.event_times["detail_clicked"]
                    - self.event_times["sale_opened"]
                )
                * 1000,
                3,
            )
        return {
            "status": "payment_page_reached_no_payment_clicked",
            "selection_mode": self.args.selection_mode,
            "selection_mode_name": MODE_NAMES[self.args.selection_mode],
            "elapsed_to_payment_seconds": self.timings["payment_page"],
            "timings": self.timings,
            "refresh_count": self.refresh_count,
            "sale_trigger": self.sale_trigger,
            "sale_to_purchase_dispatch_ms": self.sale_to_purchase_dispatch_ms,
            "sale_to_purchase_complete_ms": self.sale_to_purchase_complete_ms,
            "countdown_rearm_count": self.countdown_rearm_count,
            "countdown_last_seen_to_sale_ms": self.countdown_last_seen_to_sale_ms,
            "countdown_missing_duration_ms": self.countdown_missing_duration_ms,
            "last_refresh_to_sale_ms": self.last_refresh_to_sale_ms,
            "countdown_armed_at_trigger": self.countdown_armed_at_trigger,
            "sale_detection_to_purchase_complete_ms": (
                sale_detection_to_purchase_complete_ms
            ),
            "submit_attempts": self.submit_attempts,
            "retry_attempts": self.retry_attempts,
            "artifacts": str(self.args.artifacts.resolve()),
            "safety": "alipay_pay_button_not_clicked",
        }

    def wait_for_alipay_mode_1(self) -> dict[str, object]:
        """Retry Continue -> Immediate Submit until Alipay takes foreground."""
        deadline = time.perf_counter() + self.args.queue_timeout
        retry = self.device(resourceId=RETRY_RESOURCE, clickable=True)
        submit = self.device(
            packageName=DAMAI_PACKAGE, text="立即提交", clickable=True
        )
        pending = self.device(packageName=DAMAI_PACKAGE, text="去支付")
        phase = "await_result"

        while time.perf_counter() < deadline:
            if self.device.app_current().get("package") == ALIPAY_PACKAGE:
                return self.payment_result()

            retry_visible = retry.exists(timeout=0)
            if retry_visible:
                if phase == "await_result":
                    retry.click()
                    self.retry_attempts += 1
                    phase = "wait_retry_close"
                time.sleep(0.003)
                continue

            if phase == "wait_retry_close":
                phase = "ready_to_submit"

            if phase == "ready_to_submit" and submit.exists(timeout=0):
                submit.click()
                self.submit_attempts += 1
                phase = "await_result"
                continue

            if pending.exists(timeout=0):
                pending.click()
            time.sleep(0.003)

        raise TimeoutError(
            "模式 1 重试后未在限定时间内到达支付宝收银台："
            f"提交 {self.submit_attempts} 次，继续尝试 {self.retry_attempts} 次"
        )

    def wait_for_alipay(self) -> dict[str, object]:
        if self.args.selection_mode == 1:
            return self.wait_for_alipay_mode_1()

        deadline = time.perf_counter() + self.args.queue_timeout
        while time.perf_counter() < deadline:
            if self.device.app_current().get("package") == ALIPAY_PACKAGE:
                return self.payment_result()
            retry = self.device(resourceId=RETRY_RESOURCE)
            if retry.exists(timeout=0):
                retry.click()
            pending = self.device(packageName=DAMAI_PACKAGE, text="去支付")
            if pending.exists(timeout=0):
                pending.click()
            time.sleep(0.01)
        raise TimeoutError("立即提交后未在限定时间内到达支付宝收银台")

    def run(self) -> dict[str, object]:
        self.started = time.perf_counter()
        try:
            state = (
                self.wait_for_sale_fast()
                if self.args.fast
                else self.wait_for_sale()
                if self.args.wait_for_sale
                else self.open_ticket_panel()
            )
            if state != "order":
                if self.args.selection_mode == 1:
                    self.select_mode_1()
                elif self.args.selection_mode == 2:
                    self.select_mode_2()
                else:
                    self.select_mode_3()
                self.enter_order_page()

            if self.args.stop_before_submit:
                return {
                    "status": "stopped_on_order_page_before_submit",
                    "selection_mode": self.args.selection_mode,
                    "selection_mode_name": MODE_NAMES[self.args.selection_mode],
                    "target_price": self.target_price,
                    "elapsed_to_order_page_seconds": self.timings.get("order_page", 0),
                    "timings": self.timings,
                    "refresh_count": self.refresh_count,
                    "sale_trigger": self.sale_trigger,
                    "countdown_rearm_count": self.countdown_rearm_count,
                    "countdown_last_seen_to_sale_ms": (
                        self.countdown_last_seen_to_sale_ms
                    ),
                    "countdown_missing_duration_ms": (
                        self.countdown_missing_duration_ms
                    ),
                    "last_refresh_to_sale_ms": self.last_refresh_to_sale_ms,
                    "countdown_armed_at_trigger": self.countdown_armed_at_trigger,
                    "artifacts": str(self.args.artifacts.resolve()),
                    "safety": "immediate_submit_not_clicked; alipay_not_opened",
                }

            self.submit_order()
            return self.wait_for_alipay()
        finally:
            self.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    result = CheckoutFlow(args).run()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
