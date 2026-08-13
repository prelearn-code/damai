from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import uiautomator2 as u2


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
# horizontally centered; its projected center is approximately y=1470.
RETRY_DIALOG_POINT = (720, 1470)

# All timing knobs are kept together for quick tuning.
PRECLICK_LEAD_S = 2.0
CLICK_INTERVAL_S = 0.150
FIRST_STATE_CHECK_AFTER_S = 2.0
STATE_CHECK_INTERVAL_S = 1.0
MAX_RUNTIME_AFTER_SALE_S = 10 * 60.0
THREAD_JOIN_TIMEOUT_S = 3.0

# UI identifiers used only for in-memory state checks.
PURCHASE_RESOURCE = "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
ORDER_TITLE_RESOURCE = "cn.damai:id/order_activity_title"


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
    foreground_check_errors: int = 0
    worker_error: str | None = None
    first_tap_wall_time: str | None = None
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

    def record_probe(self, page: str) -> None:
        with self.lock:
            self.probe_count += 1
            self.last_page = page

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
            return {
                "status": self.status,
                "last_package": self.last_package,
                "last_page": self.last_page,
                "main_tap_count": self.main_tap_count,
                "retry_tap_count": self.retry_tap_count,
                "probe_count": self.probe_count,
                "foreground_check_errors": self.foreground_check_errors,
                "worker_error": self.worker_error,
                "first_tap_wall_time": self.first_tap_wall_time,
            }


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


class FastPageProbe:
    """Read only the minimum selectors needed by mode 0; never dump the UI tree."""

    def __init__(self, device: object) -> None:
        self.device = device
        self.order_title = device(resourceId=ORDER_TITLE_RESOURCE)

    def read(self) -> tuple[str, str]:
        package = str(self.device.app_current().get("package", ""))
        if package != DAMAI_PACKAGE:
            return package, classify_fast_state(package)

        # The user-defined mode-0 rule treats remaining on the confirmation
        # page after the first probe as evidence that the retry dialog blocked
        # submission. Do not query an unverified dialog button resource id.
        order_page_visible = self.order_title.exists(timeout=0)
        return package, classify_fast_state(
            package,
            order_page_visible=order_page_visible,
        )


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
    retry_requested: threading.Event,
    tap_shell: PersistentTapShell,
) -> None:
    try:
        device = u2.connect(DEVICE_SERIAL)
        if not wait_until(schedule.click_start_monotonic, stop_event):
            return

        next_tap = max(schedule.click_start_monotonic, time.perf_counter())
        state.finish("clicking")
        while not stop_event.is_set():
            now = time.perf_counter()
            if now >= schedule.deadline_monotonic:
                state.finish("timeout")
                stop_event.set()
                return
            if not wait_until(next_tap, stop_event):
                return

            try:
                package = str(device.app_current().get("package", ""))
            except Exception:
                # A failed foreground check means no coordinate tap is safe.
                state.record_foreground_error()
                next_tap = time.perf_counter() + CLICK_INTERVAL_S
                continue

            state.update_package(package)
            if package == ALIPAY_PACKAGE:
                state.finish("alipay_reached")
                stop_event.set()
                return
            if package != DAMAI_PACKAGE:
                next_tap = time.perf_counter() + CLICK_INTERVAL_S
                continue

            retry = retry_requested.is_set()
            point = RETRY_DIALOG_POINT if retry else MAIN_ACTION_POINT
            tap_shell.tap(point)
            state.record_tap(retry=retry)
            if retry:
                retry_requested.clear()

            next_tap += CLICK_INTERVAL_S
            completed_at = time.perf_counter()
            if next_tap <= completed_at:
                next_tap = completed_at + CLICK_INTERVAL_S
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
    if not device(resourceId=PURCHASE_RESOURCE).exists(timeout=0.3):
        raise RuntimeError("未检测到详情页购票入口，请先停在目标演唱会详情页")


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
    retry_requested = threading.Event()
    tap_shell = PersistentTapShell(ADB_PATH, DEVICE_SERIAL)
    worker = threading.Thread(
        target=tap_loop,
        args=(schedule, state, stop_event, retry_requested, tap_shell),
        name="damai-mode0-tap-loop",
        daemon=True,
    )
    worker.start()

    next_probe = max(schedule.first_probe_monotonic, time.perf_counter())
    try:
        while not stop_event.is_set():
            now = time.perf_counter()
            if now >= schedule.deadline_monotonic:
                state.finish("timeout")
                stop_event.set()
                break
            if not wait_until(next_probe, stop_event):
                break

            try:
                package, page = page_probe.read()
            except Exception as exc:
                package = ""
                page = f"probe_error:{type(exc).__name__}"
            state.update_package(package)
            state.record_probe(page)

            probe_record = {
                "event": "state_probe",
                "wall_time": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
                "seconds_after_sale": round(
                    time.perf_counter() - schedule.sale_monotonic,
                    3,
                ),
                "package": package,
                "page": page,
            }
            print(json.dumps(probe_record, ensure_ascii=False), flush=True)

            if page == "alipay" or package == ALIPAY_PACKAGE:
                state.finish("alipay_reached")
                stop_event.set()
                break
            # Probes begin two seconds after the supplied sale time. If the
            # confirmation page is still present, request one tap on the
            # inferred Continue Trying point; the 150 ms loop then resumes the
            # shared main action point automatically.
            if page == "order_page":
                retry_requested.set()

            next_probe += STATE_CHECK_INTERVAL_S
            completed_at = time.perf_counter()
            if next_probe <= completed_at:
                next_probe = completed_at + STATE_CHECK_INTERVAL_S
    finally:
        stop_event.set()
        worker.join(timeout=THREAD_JOIN_TIMEOUT_S)
        tap_shell.close()

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
            "safety": "stops_on_alipay_foreground; never clicks alipay_pay",
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
