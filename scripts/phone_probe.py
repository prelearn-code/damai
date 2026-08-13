from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import uiautomator2 as u2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Android UI probe")
    parser.add_argument("--serial", required=True, help="ADB device serial")
    parser.add_argument("--package", default="cn.damai", help="Target package name")
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path("artifacts"),
        help="Directory for screenshot and hierarchy output",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    args = parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)

    device = u2.connect(args.serial)
    info = device.info
    current = device.app_current()

    screenshot_path = args.artifacts / "phone-current.png"
    hierarchy_path = args.artifacts / "phone-current.xml"
    report_path = args.artifacts / "phone-current.json"
    device.screenshot(str(screenshot_path))
    hierarchy = device.dump_hierarchy(compressed=False, pretty=True)
    hierarchy_path.write_text(hierarchy, encoding="utf-8")

    visible_nodes: list[dict[str, str]] = []
    root = ET.fromstring(hierarchy)
    for node in root.iter("node"):
        text = node.attrib.get("text", "").strip()
        resource_id = node.attrib.get("resource-id", "").strip()
        description = node.attrib.get("content-desc", "").strip()
        if not (text or resource_id or description):
            continue
        visible_nodes.append(
            {
                "text": text,
                "resource_id": resource_id,
                "description": description,
                "class": node.attrib.get("class", ""),
                "clickable": node.attrib.get("clickable", ""),
                "enabled": node.attrib.get("enabled", ""),
                "bounds": node.attrib.get("bounds", ""),
            }
        )

    result = {
        "serial": args.serial,
        "device": {
            "product_name": info.get("productName"),
            "sdk": info.get("sdkInt"),
            "display": [info.get("displayWidth"), info.get("displayHeight")],
            "screen_on": info.get("screenOn"),
        },
        "current_app": current,
        "target_package_is_foreground": current.get("package") == args.package,
        "visible_node_count": len(visible_nodes),
        "visible_nodes": visible_nodes[:200],
        "artifacts": {
            "screenshot": str(screenshot_path.resolve()),
            "hierarchy": str(hierarchy_path.resolve()),
            "report": str(report_path.resolve()),
        },
    }
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console_result = {**result, "visible_nodes": visible_nodes[:30]}
    print(json.dumps(console_result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
