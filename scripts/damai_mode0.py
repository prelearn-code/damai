from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from xml.etree import ElementTree as ET

import uiautomator2 as u2

from damai_android_tail import run_tail_probe_loop


# Device and safety configuration.
DEVICE_SERIAL = "8595251f"
EXPECTED_SCREEN_SIZE = (1440, 3200)
ADB_PATH = Path(r"E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe")
DAMAI_PACKAGE = "cn.damai"
ALIPAY_PACKAGE = "com.eg.android.AlipayGphone"

# Calibrated/inferred tap points for 1440 x 3200 only.
# MAIN_ACTION_POINT is inside all three proven bottom-right actions:
# detail purchase entry, ticket-panel Confirm, and order-page Immediate Submit.
MAIN_ACTION_POINT = (1119, 3088)
# Inferred from the supplied photographed dialog. The primary retry button is
# [244,1891][1195,2041] in every Android retry-dialog capture under
# artifacts/8_14. Use the measured center, not the old photographed estimate.
RETRY_DIALOG_POINT = (720, 1966)

# All timing knobs are kept together for quick tuning.
PRECLICK_LEAD_S = 2.0
CLICK_INTERVAL_S = 0.150
FIRST_STATE_CHECK_AFTER_S = 0.0
STATE_CHECK_INTERVAL_S = 0.0
RISK_STATE_CHECK_INTERVAL_S = 0.25
MAX_RUNTIME_AFTER_SALE_S = 10 * 60.0
THREAD_JOIN_TIMEOUT_S = 3.0

# UI identifiers used only for in-memory state checks.
ORDER_TITLE_RESOURCE = "cn.damai:id/order_activity_title"
RETRY_RESOURCE = "cn.damai:id/damai_theme_dialog_confirm_btn"
BACK_REDIRECT_RESOURCE = "cn.damai:id/damai_theme_dialog_cancel_btn"
RISK_RESOURCE = "cn.damai:id/water_mark_view_id"
SUBMIT_TEXT = "立即提交"

PAGE_RETRY_DIALOG = "retry_dialog"
PAGE_TERMINAL_DIALOG = "terminal_dialog"
PAGE_ORDER_PAGE = "order_page"
PAGE_ORDER_LOADING = "order_page_loading"
PAGE_RISK_VERIFICATION = "risk_verification"
PAGE_DAMAI_OTHER = "damai_other"


@dataclass(frozen=True)
class Mode0Schedule:
    sale_monotonic: float
    click_start_monotonic: float
    first_probe_monotonic: float
    deadline_monotonic: float


@dataclass
class RuntimeState:
    status: str = "waiting"
    last_package: str = ""
    last_page: str = "unknown"
    main_tap_count: int = 0
    retry_tap_count: int = 0
    probe_count: int = 0
    retry_dialog_hits: int = 0
    terminal_dialog_hits: int = 0
    risk_verification_hits: int = 0
    risk_back_count: int = 0
    risk_wait_seconds: float = 0.0
    state_transitions: list[dict[str, object]] = field(default_factory=list)
    foreground_check_errors: int = 0
    worker_error: str | None = None
    first_tap_wall_time: str | None = None
    risk_wait_started: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_package(self, package: str) -> None:
        with self.lock:
            self.last_package = package

    def record_tap(self, retry: bool) -> None:
        with self.lock:
            if self.first_tap_wall_time is None:
                self.first_tap_wall_time = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
            if retry:
                self.retry_tap_count += 1
            else:
                self.main_tap_count += 1

    def record_risk_back(self) -> None:
        with self.lock:
            self.risk_back_count += 1

    def record_probe(self, page: str) -> None:
        with self.lock:
            self.probe_count += 1
            self.last_page = page

    def record_dialog(self, page: str) -> None:
        with self.lock:
            if page == PAGE_RETRY_DIALOG:
                self.retry_dialog_hits += 1
            elif page == PAGE_TERMINAL_DIALOG:
                self.terminal_dialog_hits += 1

    def transition(self, previous: str, current: str, page: str) -> None:
        if previous == current:
            return
        with self.lock:
            self.state_transitions.append(
                {
                    "from": previous,
                    "to": current,
                    "page": page,
                    "wall_time": datetime.now().astimezone().isoformat(
                        timespec="milliseconds"
                    ),
                }
            )

    def enter_risk_wait(self, now: float) -> None:
        with self.lock:
            if self.risk_wait_started is None:
                self.risk_verification_hits += 1
                self.risk_wait_started = now

    def leave_risk_wait(self, now: float) -> None:
        with self.lock:
            if self.risk_wait_started is not None:
                self.risk_wait_seconds += now - self.risk_wait_started
                self.risk_wait_started = None

    def record_foreground_error(self) -> None:
        with self.lock:
            self.foreground_check_errors += 1

    def finish(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            if error is not None:
                self.worker_error = error

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            risk_wait_seconds = self.risk_wait_seconds
            if self.risk_wait_started is not None:
                risk_wait_seconds += time.perf_counter() - self.risk_wait_started
            return {
                "status": self.status,
                "last_package": self.last_package,
                "last_page": self.last_page,
                "main_tap_count": self.main_tap_count,
                "retry_tap_count": self.retry_tap_count,
                "probe_count": self.probe_count,
                "retry_dialog_hits": self.retry_dialog_hits,
                "terminal_dialog_hits": self.terminal_dialog_hits,
                "risk_verification_hits": self.risk_verification_hits,
                "risk_back_count": self.risk_back_count,
                "risk_wait_seconds": round(risk_wait_seconds, 3),
                "state_transitions": list(self.state_transitions),
                "foreground_check_errors": self.foreground_check_errors,
                "foreground_query_errors": self.foreground_check_errors,
                "worker_error": self.worker_error,
                "first_tap_wall_time": self.first_tap_wall_time,
            }


@dataclass(frozen=True)
class StateDecision:
    previous_phase: str
    phase: str
    action: str | None = None
    status: str | None = None
    clear_pending: bool = False
    dialog_hit: str | None = None
    entered_risk: bool = False
    left_risk: bool = False


class Mode0StateController:
    """Pure state machine for the post-submit retry loop."""

    def __init__(self) -> None:
        self.phase = "blind"
        self.terminal_streak = 0

    @property
    def controlled(self) -> bool:
        return self.phase != "blind"

    def observe(self, page: str) -> StateDecision:
        previous = self.phase
        entered_risk = False
        left_risk = False

        if page == PAGE_RISK_VERIFICATION:
            action = None
            if self.phase != "risk_back_requested":
                self.phase = "risk_back_requested"
                entered_risk = True
                action = "back"
            self.terminal_streak = 0
            return StateDecision(
                previous,
                self.phase,
                action=action,
                clear_pending=True,
                entered_risk=entered_risk,
            )

        if self.phase == "risk_back_requested":
            if page not in (
                PAGE_RETRY_DIALOG,
                PAGE_TERMINAL_DIALOG,
                PAGE_ORDER_PAGE,
                PAGE_ORDER_LOADING,
            ):
                return StateDecision(previous, self.phase, clear_pending=True)
            left_risk = True
            self.phase = (
                "await_result"
                if page == PAGE_TERMINAL_DIALOG
                else "await_submit"
            )

        if page == PAGE_RETRY_DIALOG:
            self.terminal_streak = 0
            if self.phase != "retry_clicked":
                self.phase = "retry_clicked"
                return StateDecision(
                    previous,
                    self.phase,
                    action="retry",
                    dialog_hit=PAGE_RETRY_DIALOG,
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if page == PAGE_TERMINAL_DIALOG:
            if self.phase == "await_result":
                self.terminal_streak += 1
                status = (
                    "terminal_no_retry_option"
                    if self.terminal_streak >= 2
                    else None
                )
                return StateDecision(
                    previous,
                    self.phase,
                    status=status,
                    clear_pending=True,
                    dialog_hit=(
                        PAGE_TERMINAL_DIALOG if self.terminal_streak == 1 else None
                    ),
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        self.terminal_streak = 0
        if page == PAGE_ORDER_PAGE:
            if self.phase != "await_result":
                self.phase = "await_result"
                return StateDecision(
                    previous,
                    self.phase,
                    action="submit",
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if page == PAGE_ORDER_LOADING:
            if self.phase in ("blind", "retry_clicked"):
                self.phase = "await_submit"
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if self.phase == "retry_clicked":
            self.phase = "await_submit"
        return StateDecision(previous, self.phase, left_risk=left_risk)


class TapRequestQueue:
    """Single-slot request queue; the ADB worker is its only consumer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: str | None = None
        self.changed = threading.Event()

    def request(self, action: str) -> bool:
        if action not in ("main", "submit", "retry", "back"):
            raise ValueError(f"未知点击动作：{action}")
        with self._lock:
            if self._pending is not None:
                return False
            self._pending = action
            self.changed.set()
            return True

    def pop(self) -> str | None:
        with self._lock:
            action = self._pending
            self._pending = None
            self.changed.clear()
            return action

    def clear(self) -> None:
        with self._lock:
            self._pending = None
            self.changed.clear()


class PersistentTapShell:
    """Dispatch low-overhead ADB taps through one persistent device shell."""

    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None:
            raise RuntimeError("无法创建持久 ADB shell")

    def tap(self, point: tuple[int, int]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.process.stdin is not None
        self.process.stdin.write(f"input tap {point[0]} {point[1]}\n")
        self.process.stdin.flush()

    def back(self) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.process.stdin is not None
        self.process.stdin.write("input keyevent 4\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


# Lightweight foreground query: parse only the mCurrentFocus / mFocusedApp
# lines instead of the full `dumpsys window windows` output that
# app_current() transfers back over adb.
FOCUS_LINE_RE = re.compile(r"mCurrentFocus=Window\{[^}]*\s+u\d+\s+([^\s/]+)/")
FOCUSED_APP_RE = re.compile(r"mFocusedApp=ActivityRecord\{[^}]*\s+u\d+\s+([^\s/]+)/")


def parse_foreground_package(line: str) -> str | None:
    """Extract the foreground package from one dumpsys window line."""
    match = FOCUS_LINE_RE.search(line)
    if match:
        return match.group(1)
    match = FOCUSED_APP_RE.search(line)
    if match:
        return match.group(1)
    return None


class PersistentQueryShell:
    """One persistent adb shell for lightweight foreground queries.

    Runs `dumpsys window | grep -E "mCurrentFocus|mFocusedApp"` on the device
    and reads back only the matched lines (terminated by a __EOT__ marker),
    avoiding the full dumpsys round trip of app_current(). Independent of the
    tap shell: taps are fire-and-forget, this shell owns its stdout stream.
    """

    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("无法创建持久 ADB 查询 shell")

    def query_package(self) -> str | None:
        """Return the current foreground package, or None when unparseable."""
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB 查询 shell 已意外结束")
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; echo __EOT__\n"
        )
        self.process.stdin.flush()
        package: str | None = None
        while True:
            line = self.process.stdout.readline()
            if line.strip() == "__EOT__":
                return package
            parsed = parse_foreground_package(line)
            if parsed is not None and package is None:
                package = parsed

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


def parse_sale_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "时间格式应为 YYYY-MM-DD HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone Damai mode 0: repeatedly tap the shared bottom action "
            "and stop immediately when Alipay becomes foreground."
        )
    )
    parser.add_argument(
        "sale_time",
        type=parse_sale_time,
        help='Local start time, for example: "2026-08-18 13:00:00"',
    )
    return parser.parse_args()


def make_schedule(
    sale_time: datetime,
    now_wall: datetime,
    now_monotonic: float,
) -> Mode0Schedule:
    seconds_to_sale = (sale_time - now_wall).total_seconds()
    sale_monotonic = now_monotonic + seconds_to_sale
    return Mode0Schedule(
        sale_monotonic=sale_monotonic,
        click_start_monotonic=sale_monotonic - PRECLICK_LEAD_S,
        first_probe_monotonic=sale_monotonic + FIRST_STATE_CHECK_AFTER_S,
        deadline_monotonic=sale_monotonic + MAX_RUNTIME_AFTER_SALE_S,
    )


def classify_fast_state(
    package: str,
    order_page_visible: bool = False,
) -> str:
    if package == ALIPAY_PACKAGE:
        return "alipay"
    if package != DAMAI_PACKAGE:
        return "other_app"
    if order_page_visible:
        return "order_page"
    return "damai_other"


def classify_hierarchy_xml(xml_text: str) -> str:
    """Classify a saved Android hierarchy without talking to a device."""
    root = ET.fromstring(xml_text)
    nodes = list(root.iter("node"))

    def has_resource(resource_id: str, *, clickable: bool | None = None) -> bool:
        for node in nodes:
            if node.attrib.get("resource-id") != resource_id:
                continue
            if clickable is None or node.attrib.get("clickable") == str(clickable).lower():
                return True
        return False

    retry_visible = has_resource(RETRY_RESOURCE, clickable=True)
    if retry_visible:
        return PAGE_RETRY_DIALOG
    if has_resource(BACK_REDIRECT_RESOURCE, clickable=True):
        return PAGE_TERMINAL_DIALOG
    if any(
        node.attrib.get("package") == DAMAI_PACKAGE
        and node.attrib.get("text") == SUBMIT_TEXT
        and node.attrib.get("clickable") == "true"
        for node in nodes
    ):
        return PAGE_ORDER_PAGE
    if has_resource(ORDER_TITLE_RESOURCE):
        return PAGE_ORDER_LOADING
    if has_resource(RISK_RESOURCE):
        return PAGE_RISK_VERIFICATION
    return PAGE_DAMAI_OTHER


class FastPageProbe:
    """Probe verified selectors only; never dump or save the UI tree."""

    def __init__(self, device: object) -> None:
        self.device = device
        self.order_title = device(resourceId=ORDER_TITLE_RESOURCE)
        self.retry = device(resourceId=RETRY_RESOURCE, clickable=True)
        self.back_redirect = device(
            resourceId=BACK_REDIRECT_RESOURCE,
            clickable=True,
        )
        self.submit = device(
            packageName=DAMAI_PACKAGE,
            text=SUBMIT_TEXT,
            clickable=True,
        )
        self.risk = device(resourceId=RISK_RESOURCE)

    def read(self, phase: str) -> str:
        submit_checked = False
        if phase in ("retry_clicked", "await_submit"):
            submit_checked = True
            if self.submit.exists(timeout=0):
                return PAGE_ORDER_PAGE
        if self.retry.exists(timeout=0):
            return PAGE_RETRY_DIALOG
        if phase in ("await_result", "risk_back_requested") and self.back_redirect.exists(
            timeout=0
        ):
            return PAGE_TERMINAL_DIALOG
        if not submit_checked and self.submit.exists(timeout=0):
            return PAGE_ORDER_PAGE
        if phase in ("await_result", "risk_back_requested") and self.risk.exists(timeout=0):
            return PAGE_RISK_VERIFICATION
        if self.order_title.exists(timeout=0):
            return PAGE_ORDER_LOADING
        return PAGE_DAMAI_OTHER


def wait_until(target: float, stop_event: threading.Event) -> bool:
    while not stop_event.is_set():
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 0.1))
    return False


def tap_loop(
    schedule: Mode0Schedule,
    state: RuntimeState,
    stop_event: threading.Event,
    controlled_event: threading.Event,
    requests: TapRequestQueue,
    tap_shell: PersistentTapShell,
    query_shell: PersistentQueryShell,
) -> None:
    try:
        if not wait_until(schedule.click_start_monotonic, stop_event):
            return

        next_blind_tap = max(schedule.click_start_monotonic, time.perf_counter())
        state.finish("clicking")
        while not stop_event.is_set():
            now = time.perf_counter()
            if now >= schedule.deadline_monotonic:
                state.finish("timeout")
                stop_event.set()
                return

            try:
                package = query_shell.query_package()
            except Exception:
                # A failed foreground check means no coordinate tap is safe.
                state.record_foreground_error()
                stop_event.wait(0.01)
                continue
            if package is None:
                # Unparseable foreground means unknown: still no tap.
                state.record_foreground_error()
                stop_event.wait(0.01)
                continue

            state.update_package(package)
            if package == ALIPAY_PACKAGE:
                requests.clear()
                state.finish("alipay_reached")
                stop_event.set()
                return
            if package != DAMAI_PACKAGE:
                stop_event.wait(0.01)
                continue

            action = requests.pop()
            now = time.perf_counter()
            if action is None and not controlled_event.is_set() and now >= next_blind_tap:
                action = "main"
                next_blind_tap += CLICK_INTERVAL_S
                if next_blind_tap <= now:
                    next_blind_tap = now + CLICK_INTERVAL_S

            if action is not None:
                if action == "back":
                    tap_shell.back()
                    state.record_risk_back()
                    continue
                retry = action == "retry"
                point = RETRY_DIALOG_POINT if retry else MAIN_ACTION_POINT
                tap_shell.tap(point)
                state.record_tap(retry=retry)
                continue

            if controlled_event.is_set():
                requests.changed.wait(0.01)
            else:
                stop_event.wait(min(max(next_blind_tap - now, 0.0), 0.01))
    except BaseException as exc:
        state.finish("error", f"{type(exc).__name__}: {exc}")
        stop_event.set()


def validate_preflight(device: object) -> None:
    actual_size = tuple(device.window_size())
    if actual_size != EXPECTED_SCREEN_SIZE:
        raise RuntimeError(
            f"手机分辨率为 {actual_size[0]}×{actual_size[1]}，"
            f"但模式 0 坐标按 {EXPECTED_SCREEN_SIZE[0]}×{EXPECTED_SCREEN_SIZE[1]} 校准"
        )
    current_package = str(device.app_current().get("package", ""))
    if current_package != DAMAI_PACKAGE:
        raise RuntimeError("请先将大麦置于前台并停在目标演唱会详情页")


def run(sale_time: datetime) -> dict[str, object]:
    if not ADB_PATH.is_file():
        raise FileNotFoundError(f"ADB 不存在：{ADB_PATH}")

    probe_device = u2.connect(DEVICE_SERIAL)
    validate_preflight(probe_device)
    page_probe = FastPageProbe(probe_device)
    schedule = make_schedule(sale_time, datetime.now(), time.perf_counter())
    if time.perf_counter() >= schedule.deadline_monotonic:
        return {
            "status": "timeout_before_start",
            "sale_time": sale_time.isoformat(sep=" "),
            "safety": "no_tap_dispatched",
        }

    state = RuntimeState()
    stop_event = threading.Event()
    controlled_event = threading.Event()
    requests = TapRequestQueue()
    controller = Mode0StateController()
    tap_shell = PersistentTapShell(ADB_PATH, DEVICE_SERIAL)
    query_shell = PersistentQueryShell(ADB_PATH, DEVICE_SERIAL)
    worker = threading.Thread(
        target=tap_loop,
        args=(
            schedule,
            state,
            stop_event,
            controlled_event,
            requests,
            tap_shell,
            query_shell,
        ),
        name="damai-mode0-tap-loop",
        daemon=True,
    )
    worker.start()

    try:
        run_tail_probe_loop(
            schedule=schedule,
            state=state,
            stop_event=stop_event,
            controlled_event=controlled_event,
            requests=requests,
            controller=controller,
            page_probe=page_probe,
            wait_until=wait_until,
            state_check_interval_s=STATE_CHECK_INTERVAL_S,
            risk_state_check_interval_s=RISK_STATE_CHECK_INTERVAL_S,
        )
    finally:
        requests.clear()
        state.leave_risk_wait(time.perf_counter())
        stop_event.set()
        worker.join(timeout=THREAD_JOIN_TIMEOUT_S)
        tap_shell.close()
        query_shell.close()

    result = state.snapshot()
    result.update(
        {
            "sale_time": sale_time.isoformat(sep=" "),
            "finished_at": datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            "seconds_after_sale": round(
                time.perf_counter() - schedule.sale_monotonic,
                3,
            ),
            "main_action_point": list(MAIN_ACTION_POINT),
            "retry_dialog_point": list(RETRY_DIALOG_POINT),
            "safety": (
                "stops_on_alipay_foreground_or_terminal_dialog; "
                "backs_once_from_risk_verification; "
                "never_clicks_alipay_pay; never_clicks_back_redirect"
            ),
        }
    )
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    result = run(args.sale_time)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
