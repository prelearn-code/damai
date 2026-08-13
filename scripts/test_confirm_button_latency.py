from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time

import uiautomator2 as u2


ADB = Path(r"E:\soft-data\Android_SDK_DIR\platform-tools\adb.exe")
SERIAL = "8595251f"
PURCHASE_RESOURCE = "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
CONFIRM_RESOURCE = "cn.damai:id/btn_buy_view"
PURCHASE_POINT = (898, 3082)
POLL_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.01


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("profiles/confirm_button_latency_test.json"),
    )
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    delay_ms = float(config.get("detection_to_click_delay_ms", 0))
    if delay_ms < 0:
        raise ValueError("detection_to_click_delay_ms 不能小于 0")

    device = u2.connect(SERIAL)
    if not device(resourceId=PURCHASE_RESOURCE).exists(timeout=0.2):
        raise RuntimeError("当前不在演唱会详情页")

    shell = subprocess.Popen(
        [str(ADB), "-s", SERIAL, "shell"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert shell.stdin is not None
    assert shell.stdout is not None

    def tap(point: tuple[int, int], marker: str) -> None:
        shell.stdin.write(f"input tap {point[0]} {point[1]}; echo {marker}\n")
        shell.stdin.flush()
        for line in shell.stdout:
            if line.strip() == marker:
                return
        raise RuntimeError("ADB shell 在点击完成前结束")

    try:
        tap(PURCHASE_POINT, "__PURCHASE_DONE__")

        confirm = device(resourceId=CONFIRM_RESOURCE, clickable=True)
        deadline = time.perf_counter() + POLL_TIMEOUT_SECONDS
        while time.perf_counter() < deadline:
            if confirm.exists(timeout=0):
                if delay_ms:
                    time.sleep(delay_ms / 1000.0)
                confirm.click()
                print(f"已点击确认，配置延迟 {delay_ms:g}ms")
                return
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError("10 秒内未找到确认按钮")
    finally:
        shell.terminate()


if __name__ == "__main__":
    main()
