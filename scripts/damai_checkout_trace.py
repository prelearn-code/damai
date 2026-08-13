from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
import time
import traceback
from typing import Any

import damai_checkout as checkout


class TraceWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.started_ns = time.perf_counter_ns()
        self.file = path.open("w", encoding="utf-8", buffering=1)

    def write(self, event: str, **data: Any) -> None:
        now_ns = time.perf_counter_ns()
        record = {
            "event": event,
            "elapsed_ms": round((now_ns - self.started_ns) / 1_000_000, 3),
            "perf_counter_ns": now_ns,
            "wall_time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            **data,
        }
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self) -> None:
        self.file.close()


class SelectorTraceProxy:
    def __init__(
        self, selector: Any, trace: TraceWriter, selector_args: dict[str, Any]
    ) -> None:
        self._selector = selector
        self._trace = trace
        self._selector_args = selector_args

    def click(self, *args: Any, **kwargs: Any) -> Any:
        self._trace.write("selector_click_dispatch", selector=self._selector_args)
        try:
            result = self._selector.click(*args, **kwargs)
        except BaseException as exc:
            self._trace.write(
                "selector_click_error",
                selector=self._selector_args,
                error=repr(exc),
            )
            raise
        self._trace.write("selector_click_complete", selector=self._selector_args)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._selector, name)


class DeviceTraceProxy:
    def __init__(self, device: Any, trace: TraceWriter) -> None:
        self._device = device
        self._trace = trace

    def __call__(self, *args: Any, **kwargs: Any) -> SelectorTraceProxy:
        selector = self._device(*args, **kwargs)
        selector_args = {"args": list(args), **kwargs}
        return SelectorTraceProxy(selector, self._trace, selector_args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._device, name)


class TracedCheckoutFlow(checkout.CheckoutFlow):
    def __init__(self, args: Any, trace: TraceWriter) -> None:
        self.trace = trace
        self.trace.write("flow_init_start")
        super().__init__(args)
        self.device = DeviceTraceProxy(self.device, trace)
        self.trace.write("flow_init_complete")

    def mark(self, name: str) -> None:
        super().mark(name)
        self.trace.write("flow_mark", name=name, flow_elapsed_s=self.timings[name])

    def adb_taps(
        self,
        *tap_points: tuple[int, int],
        initial_delay: float = 0.0,
        between_delay: float | tuple[float, ...] = 0.0,
    ) -> None:
        points = [list(point) for point in tap_points]
        self.trace.write(
            "adb_taps_dispatch",
            points=points,
            initial_delay_s=initial_delay,
            between_delay_s=between_delay,
        )
        try:
            super().adb_taps(
                *tap_points,
                initial_delay=initial_delay,
                between_delay=between_delay,
            )
        except BaseException as exc:
            self.trace.write("adb_taps_error", points=points, error=repr(exc))
            raise
        self.trace.write("adb_taps_complete", points=points)

    def adb_swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> None:
        self.trace.write(
            "adb_swipe_dispatch",
            start=list(start),
            end=list(end),
            duration_ms=duration_ms,
        )
        super().adb_swipe(start, end, duration_ms)
        self.trace.write("adb_swipe_complete")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    args = checkout.parse_args()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trace_path = args.artifacts / f"precision-trace-{timestamp}.jsonl"
    trace = TraceWriter(trace_path)
    trace.write(
        "trace_started",
        profile=str(args.profile),
        serial=args.serial,
        selection_mode=args.selection_mode,
        sale_time=args.sale_time,
    )

    try:
        result = TracedCheckoutFlow(args, trace).run()
    except BaseException as exc:
        trace.write(
            "flow_error",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        print(f"精确计时数据已保存：{trace_path.resolve()}", file=sys.stderr)
        raise
    else:
        trace.write("flow_result", result=result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"精确计时数据已保存：{trace_path.resolve()}")
    finally:
        trace.close()


if __name__ == "__main__":
    main()
