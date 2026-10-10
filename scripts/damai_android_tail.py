from __future__ import annotations

from datetime import datetime
import json
import time
from typing import Callable


def run_tail_probe_loop(
    *,
    schedule: object,
    state: object,
    stop_event: object,
    controlled_event: object,
    requests: object,
    controller: object,
    page_probe: object,
    wait_until: Callable[[float, object], bool],
    state_check_interval_s: float,
    risk_state_check_interval_s: float,
) -> None:
    """Run the shared Android order-page/retry state machine.

    The collaborators are deliberately passed in so mode 0 keeps its existing
    low-level worker, timings, result fields, and tests. Mode 10 enters with its
    blind-tap worker disabled and uses this exact same tail from the order page.
    """
    next_probe = max(schedule.first_probe_monotonic, time.perf_counter())
    while not stop_event.is_set():
        now = time.perf_counter()
        if now >= schedule.deadline_monotonic:
            requests.clear()
            state.finish("timeout")
            stop_event.set()
            break
        if not wait_until(next_probe, stop_event):
            break

        try:
            page = page_probe.read(controller.phase)
        except Exception as exc:
            page = f"probe_error:{type(exc).__name__}"
        state.record_probe(page)
        decision = controller.observe(page)

        if controller.controlled:
            controlled_event.set()
        state.transition(decision.previous_phase, decision.phase, page)
        if decision.dialog_hit is not None:
            state.record_dialog(decision.dialog_hit)
        if decision.entered_risk:
            state.enter_risk_wait(time.perf_counter())
        if decision.left_risk:
            state.leave_risk_wait(time.perf_counter())
        if decision.clear_pending:
            requests.clear()
        if decision.action is not None:
            requests.request(decision.action)

        if (
            decision.previous_phase != decision.phase
            or decision.action is not None
            or decision.status is not None
            or decision.dialog_hit is not None
            or page.startswith("probe_error")
        ):
            probe_record = {
                "event": "state_probe",
                "wall_time": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
                "seconds_after_sale": round(
                    time.perf_counter() - schedule.sale_monotonic,
                    3,
                ),
                "package": state.snapshot()["last_package"],
                "page": page,
                "phase": decision.phase,
                "action": decision.action,
            }
            print(json.dumps(probe_record, ensure_ascii=False), flush=True)

        if decision.status is not None:
            requests.clear()
            state.finish(decision.status)
            stop_event.set()
            break

        completed_at = time.perf_counter()
        interval = (
            risk_state_check_interval_s
            if controller.phase == "risk_back_requested"
            else state_check_interval_s
        )
        next_probe = completed_at + interval
