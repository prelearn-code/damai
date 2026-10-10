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


# Device and safety configuration (HarmonyOS, hdc tooling only).
HDC_PATH = Path(
    r"E:\soft-data\command-line-tools\sdk\default\openharmony\toolchains\hdc.exe"
)
EXPECTED_SCREEN_SIZE = (1840, 2800)
DAMAI_PACKAGE = "cn.damai.hongmeng"
ALIPAY_PACKAGE = "com.alipay.mobile.client"

# Calibrated tap points for 1840 x 2800 only.
# MAIN_ACTION_POINT is inside the intersection of all three proven bottom
# actions (bounds from artifacts/Harmony analysis):
#   detail-page 立即预订 [233,2636][1800,2737]
#   session panel 确定  [920,2602][1840,2701]
#   confirm-page 立即提交 [1500,2620][1792,2719]
# The confirm-page submit point (1646, 2670) lies in every box, so one fixed
# point keeps the Android "one point for every page" mode-0 semantics.
MAIN_ACTION_POINT = (1646, 2670)
# The "抢票人数太多啦，继续尝试别放弃" retry dialog puts its 继续尝试 button at
# [678,1444][1161,1534] (bounds from artifacts/harmony_capture/capture_001,
# identical in every dialog capture); the point below is its center. The dialog
# is modal, so taps outside its buttons land on the dim overlay and do nothing;
# the button sits 79 px above 返回重新选购 ([610,1568][1229,1651]), no cross-hit.
RETRY_DIALOG_POINT = (919, 1489)

# Page-state discriminators (text presence in one dumpLayout tree).
ORDER_TEXT = "立即提交"
# Detail pages vary by sale state: on-sale pages show 立即预订, reserved
# pre-sale pages show 已预约 / 票档已选好 … (substring match).
DETAIL_TEXTS = ("立即预订", "已预约", "票档已选好")
# The retry dialog offers 继续尝试 (keep trying) plus 返回重新选购 (go back);
# the terminal dialog ("对不起，您选购的商品库存不足，请重新选购") offers only
# 返回重新选购. Captures prove the dialog replaces the confirm-page subtree
# entirely, so 继续尝试 presence is a reliable discriminator for both.
RETRY_DIALOG_TEXT = "继续尝试"
BACK_REDIRECT_TEXT = "返回重新选购"

# All timing knobs are kept together for quick tuning.
PRECLICK_LEAD_S = 2.0
CLICK_INTERVAL_S = 0.36
# Calibrated on the pad (2026-xx): dumpLayout -i takes ~1.2 s and does NOT
# block concurrent uiInput clicks (separate hdc processes run in parallel),
# so probes may run almost back-to-back. The first probe starts at sale+2 s
# (gate): tap triggers fired before that stay pending and are released at
# the gate; afterwards probes fire on every tap (back-to-back) with a 0.5 s
# periodic backstop when taps pause.
FIRST_STATE_CHECK_AFTER_S = 2.0
STATE_CHECK_INTERVAL_S = 0.5
MAX_RUNTIME_AFTER_SALE_S = 10 * 60.0
THREAD_JOIN_TIMEOUT_S = 3.0
# Terminal dialog must be observed this many CONSECUTIVE probes before the
# run stops (Android parity: guards against a transient page or a
# classification glitch stopping the fight on a false positive).
TERMINAL_CONFIRM_COUNT = 2

# Foreground-stream knobs: the stream refreshes every ~0.2 s; a state older
# than STREAM_STALE_S is treated as stale and the tap loop falls back to a
# synchronous foreground query before any tap (safety first).
STREAM_POLL_S = 0.15
STREAM_STALE_S = 1.0

# hdc command timeouts.
HDC_TIMEOUT_S = 20.0
DUMP_TIMEOUT_S = 30.0
RECV_TIMEOUT_S = 30.0

# dumpLayout output lives on the device; recv it into the workspace root.
# (hdc file recv can refuse to create files below subdirectories under some
# sandboxes, so keep the local target at the repository root.)
DEVICE_DL_PATH = "/data/local/tmp/mode0_probe_dl.json"
LOCAL_DL_PATH = (
    Path(__file__).resolve().parent.parent / "_mode0_probe_dl.json"
)

# Probe acceleration (calibrated on the pad): dump once on the device and
# grep the key texts there, returning only small counts over hdc stdout.
# dumpLayout flags measured: -i (no merge, no filter) ~1.2 s and includes
# every window (dialog included); -a (font attributes) ~8.3 s, never use.
PROBE_DUMP_FLAG = "-i"
# stdout separator between grep results of one compound probe command.
PROBE_MARKER = "__DAMAI_PROBE__"
# Grep patterns, in classify priority order (retry before terminal, etc.).
PROBE_PATTERN_TEXTS = (
    RETRY_DIALOG_TEXT,    # 继续尝试
    BACK_REDIRECT_TEXT,   # 返回重新选购
    ORDER_TEXT,           # 立即提交
    DETAIL_TEXTS[0],      # 立即预订
)
# Device-side fixture used by the startup grep self-test.
SELF_TEST_PATH = "/data/local/tmp/mode0_grep_selftest.txt"


def grep_pattern_hex(text: str) -> str:
    """Device shell pattern for a UTF-8 needle, ASCII-safe through hdc.

    mksh ANSI-C quoting ($'...') with \\xHH escapes; the bytes are the UTF-8
    encoding of the needle, verified working on the pad (direct UTF-8 args
    also work, but this form has zero console/codepage risk).
    """
    escaped = "".join(f"\\x{byte:02x}" for byte in text.encode("utf-8"))
    return f"$'{escaped}'"


def parse_probe_counts(stdout: str, count: int) -> list[int] | None:
    """Parse the numbered segments of one compound grep probe command."""
    segments = stdout.split(PROBE_MARKER)
    values: list[int] = []
    for segment in segments[1 : 1 + count]:
        text = segment.strip()
        if not text.isdigit():
            return None
        values.append(int(text))
    if len(values) != count:
        return None
    return values


def update_terminal_streak(page: str, streak: int) -> int:
    """Track consecutive terminal-dialog observations (Android parity).

    Returns streak + 1 when the page is the terminal dialog again, else 0.
    Any non-terminal page — including probe_error — resets the streak, so a
    transient misclassification can never stop the run on its own.
    """
    if page == "terminal_dialog":
        return streak + 1
    return 0


def classify_probe_counts(counts: dict[str, int]) -> str:
    """Mirror classify_damai_page() over device-grep hit counts.

    grep -c on the single-line dump JSON yields 0/1 per pattern, so the
    presence checks below are exactly the classify_damai_page() order.
    """
    if counts.get(RETRY_DIALOG_TEXT, 0) > 0:
        return "retry_dialog"
    if counts.get(BACK_REDIRECT_TEXT, 0) > 0:
        return "terminal_dialog"
    if counts.get(ORDER_TEXT, 0) > 0:
        return "order_page"
    if counts.get(DETAIL_TEXTS[0], 0) > 0:
        return "detail_page"
    return "damai_other"


def probe_self_test(hdc: HdcShell) -> bool:
    """Verify the device-side grep pipeline once at startup (read-only).

    Round-trips a Chinese needle through printf + grep on a fixture file,
    then runs one real dump + grep. False -> caller falls back to the
    recv+parse probe path.
    """
    try:
        needle = grep_pattern_hex(RETRY_DIALOG_TEXT)
        bare = needle[2:-1]  # \xHH... without the $'...' wrapping
        out = hdc.shell(
            f"printf '{bare}' > {SELF_TEST_PATH}; "
            f"grep -c {needle} {SELF_TEST_PATH}; "
            f"grep -c __DAMAI_NO_SUCH__ {SELF_TEST_PATH}",
            timeout=30,
        )
        lines = out.strip().splitlines()
        if len(lines) != 2:
            return False
        if lines[0].strip() != "1" or lines[1].strip() != "0":
            return False
        counts = hdc.grep_counts(DEVICE_DL_PATH, (grep_pattern_hex(ORDER_TEXT),))
        return counts is not None
    except Exception:
        return False

# AA dump -l mission blocks: extract the foreground bundle.
AA_BLOCK_RE = re.compile(
    r"Mission ID #(\d+).*?bundle name \[([^\]]+)\].*?"
    r"state #(FOREGROUND|BACKGROUND)",
    re.S,
)
# WMS window dump: focus window id and window rows.
FOCUS_WINDOW_RE = re.compile(r"Focus window:\s*(\d+)")
WINDOW_ROW_RE = re.compile(
    r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
    r"\s+\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]"
)

WMS_DUMP_CMD = "hidumper -s WindowManagerService -a '-a'"


def parse_aa_foreground(text: str) -> str | None:
    """Return the foreground bundle from one `aa dump -l` output, or None."""
    for match in AA_BLOCK_RE.finditer(text):
        if match.group(3) == "FOREGROUND":
            return match.group(2)
    return None


def parse_focus_window(text: str) -> str | None:
    """Return the focused window id from a WMS window dump, or None."""
    match = FOCUS_WINDOW_RE.search(text)
    return match.group(1) if match else None


def find_window_row(text: str, win_id: str) -> dict[str, object] | None:
    """Parse the WMS window row for win_id into {name, pid, win_id, size}."""
    for line in text.splitlines():
        match = WINDOW_ROW_RE.match(line.strip())
        if match and match.group(4) == win_id:
            return {
                "name": match.group(1),
                "pid": int(match.group(3)),
                "win_id": match.group(4),
                "size": (int(match.group(12)), int(match.group(13))),
            }
    return None


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
    trigger_probe_count: int = 0
    retry_dialog_hits: int = 0
    terminal_dialog_hits: int = 0
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

    def record_probe(self, page: str, triggered: bool = False) -> None:
        with self.lock:
            self.probe_count += 1
            if triggered:
                self.trigger_probe_count += 1
            self.last_page = page

    def record_retry_dialog(self) -> None:
        with self.lock:
            self.retry_dialog_hits += 1

    def record_terminal_dialog(self) -> None:
        with self.lock:
            self.terminal_dialog_hits += 1

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
                "trigger_probe_count": self.trigger_probe_count,
                "retry_dialog_hits": self.retry_dialog_hits,
                "terminal_dialog_hits": self.terminal_dialog_hits,
                "foreground_check_errors": self.foreground_check_errors,
                "worker_error": self.worker_error,
                "first_tap_wall_time": self.first_tap_wall_time,
            }


class HdcShell:
    """Run one-shot hdc commands.

    hdc has no interactive stdin shell (unlike adb), so every command spawns
    a subprocess; the first call pays ~1.5 s channel setup, subsequent calls
    are ~0.1 s thanks to hdc server connection reuse.
    """

    def __init__(self, hdc_path: Path = HDC_PATH) -> None:
        self.hdc_path = hdc_path

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.hdc_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def warmup(self) -> None:
        self._run(["shell", "echo warm"], HDC_TIMEOUT_S)

    def shell(self, cmd: str, timeout: float = HDC_TIMEOUT_S) -> str:
        return self._run(["shell", cmd], timeout).stdout

    def grep_counts(
        self,
        dev_path: str,
        patterns: tuple[str, ...],
        timeout: float = DUMP_TIMEOUT_S,
    ) -> list[int] | None:
        """One compound command: dumpLayout + one device grep per pattern.

        Returns the hit counts (grep -c: 0/1 on the single-line dump JSON,
        matching-line count in general), or None when the output cannot be
        parsed. grep exits 1 on zero matches, which is NOT an error here, so
        only stdout is parsed, never returncode.
        """
        parts = [f"uitest dumpLayout -p {dev_path} {PROBE_DUMP_FLAG}"]
        for pattern in patterns:
            parts.append(f"echo {PROBE_MARKER}")
            parts.append(f"grep -c {pattern} {dev_path}")
        parts.append(f"echo {PROBE_MARKER}")
        proc = self._run(["shell", "; ".join(parts)], timeout)
        return parse_probe_counts(proc.stdout, len(patterns))

    def tap(self, point: tuple[int, int]) -> None:
        proc = self._run(
            ["shell", f"uitest uiInput click {point[0]} {point[1]}"],
            HDC_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"uitest uiInput click 失败 rc={proc.returncode}: {proc.stdout[:200]}"
            )

    def foreground_sync(self) -> str | None:
        """Synchronous foreground bundle via one-shot `aa dump -l`."""
        return parse_aa_foreground(self.shell("aa dump -l"))

    def focus_window_size(self) -> tuple[int, int] | None:
        """Screen size from the focused window row of a WMS dump."""
        text = self.shell(WMS_DUMP_CMD)
        win_id = parse_focus_window(text)
        if win_id is None:
            return None
        row = find_window_row(text, win_id)
        if row is None:
            return None
        return row["size"]  # type: ignore[return-value]

    def dump_layout(self, dev_path: str = DEVICE_DL_PATH) -> None:
        self._run(
            ["shell", f"uitest dumpLayout -p {dev_path} -i"],
            DUMP_TIMEOUT_S,
        )

    def screen_cap(self, dev_path: str) -> None:
        self._run(["shell", f"uitest screenCap -p {dev_path}"], DUMP_TIMEOUT_S)

    def file_recv(self, local: Path, dev_path: str = DEVICE_DL_PATH) -> None:
        proc = self._run(["file", "recv", dev_path, str(local)], RECV_TIMEOUT_S)
        if proc.returncode != 0:
            raise RuntimeError(f"hdc file recv 失败 rc={proc.returncode}")


class ForegroundStream:
    """Long-running `aa dump -l` stream keeping the foreground bundle fresh.

    The device side loops `aa dump -l; echo __DONE__; sleep POLL`, and this
    thread parses each block. When the stream dies or goes stale, callers must
    fall back to HdcShell.foreground_sync() before tapping.
    """

    def __init__(self, hdc_path: Path = HDC_PATH, poll: float = STREAM_POLL_S) -> None:
        self.hdc_path = hdc_path
        self.poll = poll
        self._process: subprocess.Popen[str] | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bundle: str | None = None
        self._updated_at = 0.0

    def start(self) -> None:
        cmd = (
            "while true; do aa dump -l; echo __DONE__; "
            f"sleep {self.poll}; done"
        )
        self._process = subprocess.Popen(
            [str(self.hdc_path), "shell", cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if self._process.stdout is None:
            raise RuntimeError("无法创建 hdc 长驻输出流")
        threading.Thread(
            target=self._read_loop,
            name="damai-mode0-harmony-stream",
            daemon=True,
        ).start()

    def _read_loop(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        buffer = ""
        try:
            while not self._stop.is_set():
                chunk = self._process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while "__DONE__" in buffer:
                    index = buffer.index("__DONE__")
                    block, buffer = buffer[:index], buffer[index + len("__DONE__"):]
                    bundle = parse_aa_foreground(block)
                    if bundle is not None:
                        with self._lock:
                            self._bundle = bundle
                            self._updated_at = time.perf_counter()
        finally:
            with self._lock:
                self._bundle = None
                self._updated_at = 0.0

    def current(self) -> tuple[str | None, float]:
        with self._lock:
            return self._bundle, self._updated_at

    def is_stale(self) -> bool:
        bundle, updated_at = self.current()
        if bundle is None:
            return True
        return (time.perf_counter() - updated_at) > STREAM_STALE_S

    def close(self) -> None:
        self._stop.set()
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()


def find_text_node(
    node: object,
    target: str,
    substring: bool = False,
) -> bool:
    """Depth-first search for a node whose text equals (or contains) target.

    dumpLayout JSON may be a dict (single window, `-a`) or a list of windows
    (`-i`); both shapes are handled.
    """
    if isinstance(node, list):
        return any(find_text_node(item, target, substring) for item in node)
    if not isinstance(node, dict):
        return False
    attributes = node.get("attributes") or {}
    if isinstance(attributes, dict):
        text = (attributes.get("text") or "").strip()
        if text and (target in text if substring else text == target):
            return True
    for child in node.get("children") or []:
        if find_text_node(child, target, substring):
            return True
    return False


def classify_damai_page(data: object) -> str:
    """Classify one Damai dumpLayout tree into mode-0 page states (pure).

    Returns one of: retry_dialog | terminal_dialog | order_page | detail_page
    | damai_other. Check order matters: the retry dialog contains both dialog
    texts, the terminal dialog contains only 返回重新选购, and neither dialog
    tree contains 立即提交 (captures confirm the subtree is replaced).
    """
    if find_text_node(data, RETRY_DIALOG_TEXT):
        return "retry_dialog"
    if find_text_node(data, BACK_REDIRECT_TEXT):
        return "terminal_dialog"
    if find_text_node(data, ORDER_TEXT):
        return "order_page"
    if any(find_text_node(data, text, substring=True) for text in DETAIL_TEXTS):
        return "detail_page"
    return "damai_other"


class PageProbe:
    """Read only what mode 0 needs: Damai's confirm page / retry dialog state.

    Two probe paths, chosen once at startup:
      - grep mode (default when the startup self-test passes): one compound
        hdc command dumps the tree on-device and greps the four key texts,
        returning only small counts; no 400 KB JSON leaves the device.
      - recv mode (fallback): dumpLayout + hdc file recv + local parse, the
        original path, driven by the same classify_damai_page().
    """

    def __init__(
        self,
        stream: ForegroundStream,
        hdc: HdcShell,
        local_dl: Path,
        grep_mode: bool,
    ) -> None:
        self.stream = stream
        self.hdc = hdc
        self.local_dl = local_dl
        self.grep_mode = grep_mode

    def read(self) -> tuple[str, str]:
        bundle, _ = self.stream.current()
        if bundle is None:
            return "", "stream_down"
        if bundle != DAMAI_PACKAGE:
            return bundle, classify_fast_state(bundle)
        # Pass the full in-app classification through unchanged: retry_dialog
        # and terminal_dialog must reach the probe loop verbatim (regression
        # fix — they used to be flattened to damai_other here, which made the
        # script never answer the retry dialog).
        page = self._probe_damai_page()
        if page.startswith("probe_error"):
            return bundle, page
        return bundle, page

    def _probe_damai_page(self) -> str:
        if self.grep_mode:
            return self._probe_damai_page_grep()
        return self._probe_damai_page_recv()

    def _probe_damai_page_grep(self) -> str:
        try:
            counts = self.hdc.grep_counts(
                DEVICE_DL_PATH,
                tuple(grep_pattern_hex(text) for text in PROBE_PATTERN_TEXTS),
            )
        except Exception as exc:
            return f"probe_error:{type(exc).__name__}"
        if counts is None:
            return "probe_error:grep_parse"
        return classify_probe_counts(
            dict(zip(PROBE_PATTERN_TEXTS, counts))
        )

    def _probe_damai_page_recv(self) -> str:
        try:
            self.hdc.dump_layout()
            self.hdc.file_recv(self.local_dl)
            data = json.loads(self.local_dl.read_text(encoding="utf-8"))
        except Exception as exc:
            return f"probe_error:{type(exc).__name__}"
        finally:
            try:
                self.local_dl.unlink(missing_ok=True)
            except OSError:
                pass
        return classify_damai_page(data)


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
            "HarmonyOS Damai mode 0: repeatedly tap the shared bottom action, "
            "answer the 继续尝试 retry dialog when it blocks submission, and "
            "stop when Alipay becomes foreground or the dialog offers no "
            "continue option (only 返回重新选购)."
        )
    )
    parser.add_argument(
        "sale_time",
        type=parse_sale_time,
        help='Local start time, for example: "2026-08-23 21:00:00"',
    )
    parser.add_argument(
        "--first-check-after",
        type=float,
        default=FIRST_STATE_CHECK_AFTER_S,
        help=f"Seconds after sale for the first page probe (default "
        f"{FIRST_STATE_CHECK_AFTER_S:g})",
    )
    parser.add_argument(
        "--state-check-interval",
        type=float,
        default=STATE_CHECK_INTERVAL_S,
        help=f"Periodic probe backstop, measured from the previous probe's "
        f"end (default {STATE_CHECK_INTERVAL_S:g}s; tap-triggered probes "
        f"fire immediately after every submit tap, dump itself ~1.2s)",
    )
    parser.add_argument(
        "--probe-mode",
        choices=("auto", "grep", "recv"),
        default="auto",
        help="auto: device-side grep probe when the startup self-test "
        "passes, else recv+parse (default); grep/recv force the path",
    )
    parser.add_argument(
        "--terminal-confirm-count",
        type=int,
        default=TERMINAL_CONFIRM_COUNT,
        help=f"Consecutive terminal-dialog detections required before "
        f"stopping (default {TERMINAL_CONFIRM_COUNT})",
    )
    return parser.parse_args()


def make_schedule(
    sale_time: datetime,
    now_wall: datetime,
    now_monotonic: float,
    first_check_after: float = FIRST_STATE_CHECK_AFTER_S,
) -> Mode0Schedule:
    seconds_to_sale = (sale_time - now_wall).total_seconds()
    sale_monotonic = now_monotonic + seconds_to_sale
    return Mode0Schedule(
        sale_monotonic=sale_monotonic,
        click_start_monotonic=sale_monotonic - PRECLICK_LEAD_S,
        first_probe_monotonic=sale_monotonic + first_check_after,
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


def wait_until(target: float, stop_event: threading.Event) -> bool:
    while not stop_event.is_set():
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 0.1))
    return False


def wait_until_or_trigger(
    target: float,
    stop_event: threading.Event,
    trigger: threading.Event,
    not_before: float = 0.0,
) -> str:
    """Wait until target time or a tap-trigger fires, whichever comes first.

    Returns "trigger" (and clears it: taps during a probe re-set it, which
    makes probes run back-to-back while the tap loop is submitting), "time"
    (periodic fallback), or "stopped". Probes never block taps on the pad
    (calibrated: concurrent clicks keep their ~0.36 s latency), so firing a
    probe on every submit tap is safe and cuts dialog detection to roughly
    one dump round.

    `not_before` gates both paths (used for the first probe at sale+2 s):
    a trigger fired before the gate stays PENDING instead of being consumed,
    so the first probe still fires the instant the gate opens even if no new
    tap arrives at that moment.
    """
    while not stop_event.is_set():
        now = time.perf_counter()
        if trigger.is_set() and now >= not_before:
            trigger.clear()
            return "trigger"
        if now >= target and now >= not_before:
            return "time"
        next_wake = min(target, not_before)
        stop_event.wait(min(max(next_wake - now, 0.0), 0.1))
    return "stopped"


def tap_loop(
    schedule: Mode0Schedule,
    state: RuntimeState,
    stop_event: threading.Event,
    probe_trigger: threading.Event,
    stream: ForegroundStream,
    hdc: HdcShell,
) -> None:
    try:
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

            if stream.is_stale():
                # Stream degraded: a synchronous foreground check happens
                # before this tap; an unknown foreground means no tap.
                try:
                    package = hdc.foreground_sync()
                except Exception:
                    state.record_foreground_error()
                    next_tap = time.perf_counter() + CLICK_INTERVAL_S
                    continue
                if package is None:
                    state.record_foreground_error()
                    next_tap = time.perf_counter() + CLICK_INTERVAL_S
                    continue
            else:
                package, _ = stream.current()
                if package is None:
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

            hdc.tap(MAIN_ACTION_POINT)
            state.record_tap(retry=False)
            # Every tap may have produced a server result (dialog / nothing):
            # ask the probe thread to look at the page right now. The probe
            # thread answers the retry dialog itself by tapping the retry
            # point directly (faster than a flag round trip through here).
            probe_trigger.set()

            next_tap += CLICK_INTERVAL_S
            completed_at = time.perf_counter()
            if next_tap <= completed_at:
                next_tap = completed_at + CLICK_INTERVAL_S
    except BaseException as exc:
        state.finish("error", f"{type(exc).__name__}: {exc}")
        stop_event.set()


def validate_preflight(hdc: HdcShell) -> None:
    size = hdc.focus_window_size()
    if size != EXPECTED_SCREEN_SIZE:
        actual = size if size is not None else (0, 0)
        raise RuntimeError(
            f"焦点窗口尺寸为 {actual[0]}×{actual[1]}，"
            f"但模式 0 坐标按 "
            f"{EXPECTED_SCREEN_SIZE[0]}×{EXPECTED_SCREEN_SIZE[1]} 校准"
        )
    package = hdc.foreground_sync()
    if package != DAMAI_PACKAGE:
        raise RuntimeError("请先将大麦置于前台并停在目标演唱会详情页")


def run(
    sale_time: datetime,
    first_check_after: float = FIRST_STATE_CHECK_AFTER_S,
    state_check_interval: float = STATE_CHECK_INTERVAL_S,
    probe_mode: str = "auto",
    terminal_confirm_count: int = TERMINAL_CONFIRM_COUNT,
) -> dict[str, object]:
    if not HDC_PATH.is_file():
        raise FileNotFoundError(f"hdc 不存在：{HDC_PATH}")

    hdc = HdcShell(HDC_PATH)
    hdc.warmup()
    validate_preflight(hdc)

    if probe_mode == "recv":
        grep_mode = False
    elif probe_mode == "grep":
        grep_mode = True
    else:  # auto: grep when the startup self-test passes, else recv
        grep_mode = probe_self_test(hdc)
    print(
        json.dumps(
            {
                "event": "probe_mode",
                "mode": "grep" if grep_mode else "recv",
                "requested": probe_mode,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    schedule = make_schedule(
        sale_time, datetime.now(), time.perf_counter(), first_check_after
    )
    if time.perf_counter() >= schedule.deadline_monotonic:
        return {
            "status": "timeout_before_start",
            "sale_time": sale_time.isoformat(sep=" "),
            "safety": "no_tap_dispatched",
        }

    stream = ForegroundStream(HDC_PATH)
    stream.start()

    state = RuntimeState()
    stop_event = threading.Event()
    probe_trigger = threading.Event()
    worker = threading.Thread(
        target=tap_loop,
        args=(
            schedule,
            state,
            stop_event,
            probe_trigger,
            stream,
            hdc,
        ),
        name="damai-mode0-harmony-tap-loop",
        daemon=True,
    )
    worker.start()

    LOCAL_DL_PATH.parent.mkdir(parents=True, exist_ok=True)
    page_probe = PageProbe(stream, hdc, LOCAL_DL_PATH, grep_mode)
    next_probe = max(schedule.first_probe_monotonic, time.perf_counter())
    terminal_streak = 0
    try:
        while not stop_event.is_set():
            now = time.perf_counter()
            if now >= schedule.deadline_monotonic:
                state.finish("timeout")
                stop_event.set()
                break
            reason = wait_until_or_trigger(
                next_probe,
                stop_event,
                probe_trigger,
                schedule.first_probe_monotonic,  # gate: no probe before sale+2s
            )
            if reason == "stopped":
                break
            triggered = reason == "trigger"

            try:
                package, page = page_probe.read()
            except Exception as exc:
                package = ""
                page = f"probe_error:{type(exc).__name__}"
            state.update_package(package)
            state.record_probe(page, triggered=triggered)

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
                "triggered": triggered,
            }
            print(json.dumps(probe_record, ensure_ascii=False), flush=True)

            if page == "alipay" or package == ALIPAY_PACKAGE:
                state.finish("alipay_reached")
                stop_event.set()
                break

            # Terminal dialog needs CONSECUTIVE confirmations (Android
            # parity): any other page, including probe_error, resets the
            # streak, so a transient misclassification cannot stop the run.
            terminal_streak = update_terminal_streak(page, terminal_streak)
            if page == "retry_dialog":
                # The retry dialog covers the submit button: tap the
                # calibrated 继续尝试 point directly from the probe thread —
                # the dump just proved the dialog is up, so this is the
                # fastest path to close it (the tap loop keeps tapping the
                # submit point meanwhile; taps on the dialog's dim overlay
                # are harmless no-ops). A still-open dialog is tapped again
                # by the next probe, so a missed tap self-heals.
                state.record_retry_dialog()
                try:
                    hdc.tap(RETRY_DIALOG_POINT)
                    state.record_tap(retry=True)
                except Exception as exc:
                    page = f"probe_error:{type(exc).__name__}"
            if page == "terminal_dialog":
                # No continue option left (only 返回重新选购): further taps
                # cannot reach a purchase path, so stop once the streak
                # reaches the confirm count (default 2 consecutive probes).
                state.record_terminal_dialog()
                if terminal_streak >= terminal_confirm_count:
                    state.finish("terminal_no_retry_option")
                    stop_event.set()
                    break

            next_probe += state_check_interval
            completed_at = time.perf_counter()
            if next_probe <= completed_at:
                next_probe = completed_at + state_check_interval
    finally:
        stop_event.set()
        worker.join(timeout=THREAD_JOIN_TIMEOUT_S)
        stream.close()

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
            "probe_mode": "grep" if grep_mode else "recv",
            "first_check_after_s": first_check_after,
            "state_check_interval_s": state_check_interval,
            "terminal_confirm_count": terminal_confirm_count,
            "safety": (
                "stops_on_alipay_foreground_or_terminal_dialog; "
                "never clicks alipay_pay; never clicks back_redirect"
            ),
        }
    )
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    result = run(
        args.sale_time,
        first_check_after=args.first_check_after,
        state_check_interval=args.state_check_interval,
        probe_mode=args.probe_mode,
        terminal_confirm_count=args.terminal_confirm_count,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
