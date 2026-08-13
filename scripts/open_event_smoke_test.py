from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

import uiautomator2 as u2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open one event card and return")
    parser.add_argument("--serial", required=True, help="ADB device serial")
    parser.add_argument("--artist", default="林志炫", help="Visible artist text")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    args = parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    device = u2.connect(args.serial)
    before = device.app_current()
    target = device(text=args.artist)
    if not target.exists(timeout=3):
        raise RuntimeError(f"页面上未找到歌手文本：{args.artist}")

    target.click()
    time.sleep(5)
    after = device.app_current()
    screenshot_path = args.artifacts / "event-detail.png"
    hierarchy_path = args.artifacts / "event-detail.xml"
    device.screenshot(str(screenshot_path))
    hierarchy = device.dump_hierarchy(compressed=False, pretty=True)
    hierarchy_path.write_text(hierarchy, encoding="utf-8")

    texts: list[str] = []
    for node in ET.fromstring(hierarchy).iter("node"):
        value = node.attrib.get("text", "").strip()
        if value and value not in texts:
            texts.append(value)

    result = {
        "before": before,
        "after_open": after,
        "sample_texts": texts[:80],
        "screenshot": str(screenshot_path.resolve()),
        "hierarchy": str(hierarchy_path.resolve()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    device.press("back")
    time.sleep(2)


if __name__ == "__main__":
    main()
