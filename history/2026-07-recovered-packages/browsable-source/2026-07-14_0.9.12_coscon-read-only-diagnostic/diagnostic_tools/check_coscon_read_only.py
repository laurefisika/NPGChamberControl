"""Strictly read-only diagnostic for the SPECS COSCON IS UDP interface.

Run from the project root after installing editable mode:

    python diagnostic_tools/check_coscon_read_only.py

Or double-click:

    RUN_COSCON_READ_ONLY_CHECK.bat

The script sends only the read-only commands documented in the COSCON IS
manual. It cannot send Standby, Off, Operate, Degas, Reset, SetNetwork, preset
write/delete, or any other state-changing command.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow direct execution from an unpacked project even before editable install.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from npg_chamber.devices.coscon_udp import (
    DEFAULT_COSCON_HOST,
    DEFAULT_COSCON_PORT,
    CosconReadOnlyClient,
    CosconReply,
    first_integer,
)

READ_ONLY_SEQUENCE = [
    "Info",
    "Uptime",
    "GetDeviceType",
    "GetFirmwareVersion",
    "GetDeviceSerial",
    "GetNetwork",
    "GetIdentify",
    "GetStatus",
    "GetMonitorValues",
    "GetDiagnosticValues",
    "GetTargetValues",
    "ReadNumberOfPresets",
]

REQUIRED_COMMANDS = {"Info", "GetStatus"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read COSCON IS identity, network, status, live values and presets over UDP. "
            "No state-changing command is available in this diagnostic."
        )
    )
    parser.add_argument("--host", default=DEFAULT_COSCON_HOST, help="COSCON IPv4 address")
    parser.add_argument("--port", type=int, default=DEFAULT_COSCON_PORT, help="UDP port")
    parser.add_argument("--timeout", type=float, default=2.0, help="Timeout per attempt in seconds")
    parser.add_argument("--retries", type=int, default=1, help="Retries after the first attempt")
    parser.add_argument(
        "--no-presets",
        action="store_true",
        help="Do not scan the read-only preset slots",
    )
    parser.add_argument(
        "--max-preset-index",
        type=int,
        default=64,
        help="Safety cap for preset index scanning (default: 64)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("COSCON Diagnostic Reports"),
        help="Folder for JSON and text reports",
    )
    return parser.parse_args()


def print_exchange(command: str, result: dict[str, Any]) -> None:
    if result["success"]:
        print(f"[OK] {command}")
        for line in result["reply"]["raw"].splitlines() or [""]:
            print(f"     {line}")
    else:
        print(f"[ERROR] {command}: {result['error']}")


def run_command(client: CosconReadOnlyClient, command: str) -> dict[str, Any]:
    try:
        reply = client.query(command)
        return {
            "success": bool(reply.ok),
            "reply": asdict(reply),
            "error": None if reply.ok else "COSCON returned an error/failure reply.",
        }
    except Exception as exc:  # noqa: BLE001 - diagnostic must record every failure
        return {"success": False, "reply": None, "error": f"{type(exc).__name__}: {exc}"}


def safe_filename_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def save_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = safe_filename_timestamp()
    json_path = output_dir / f"coscon_read_only_diagnostic_{stamp}.json"
    txt_path = output_dir / f"coscon_read_only_diagnostic_{stamp}.txt"

    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "COSCON IS READ-ONLY UDP DIAGNOSTIC",
        "==================================",
        f"Timestamp: {report['timestamp']}",
        f"Target: {report['target']['host']}:{report['target']['port']}",
        f"Timeout: {report['target']['timeout_s']} s",
        f"Retries: {report['target']['retries']}",
        f"Overall result: {report['overall_result']}",
        "",
        "Only read-only commands were available to this program.",
        "",
    ]
    for command, result in report["commands"].items():
        lines.append(f"[{ 'OK' if result['success'] else 'ERROR' }] {command}")
        if result["reply"]:
            lines.extend(f"    {line}" for line in result["reply"]["raw"].splitlines())
        if result["error"]:
            lines.append(f"    {result['error']}")
        lines.append("")

    txt_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return json_path, txt_path


def main() -> int:
    args = parse_args()
    if args.max_preset_index < 0:
        raise SystemExit("--max-preset-index must be zero or greater.")

    print("COSCON IS read-only UDP diagnostic")
    print("==================================")
    print(f"Target: {args.host}:{args.port}")
    print("Safety mode: READ-ONLY command whitelist")
    print("No Standby, Off, Operate, Degas, Reset or configuration command can be sent.\n")

    client = CosconReadOnlyClient(
        host=args.host,
        port=args.port,
        timeout_s=args.timeout,
        retries=args.retries,
    )

    report: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": {
            "host": args.host,
            "port": args.port,
            "timeout_s": args.timeout,
            "retries": args.retries,
        },
        "safety": {
            "read_only": True,
            "state_changing_commands_available": False,
        },
        "commands": {},
        "overall_result": "pending",
    }

    for command in READ_ONLY_SEQUENCE:
        result = run_command(client, command)
        report["commands"][command] = result
        print_exchange(command, result)

    if not args.no_presets:
        count_result = report["commands"]["ReadNumberOfPresets"]
        count = None
        if count_result["reply"]:
            count = first_integer(count_result["reply"]["raw"])

        if count is None or count < 0:
            print("[INFO] Preset scan skipped: the number of preset slots could not be read.")
        else:
            upper = min(count, args.max_preset_index)
            print(
                f"[INFO] Read-only preset scan: indices 0 through {upper} "
                "(both zero- and one-based firmware layouts are covered)."
            )
            for index in range(0, upper + 1):
                command = f"ReadPreset {index}"
                result = run_command(client, command)
                report["commands"][command] = result
                print_exchange(command, result)

    required_ok = all(
        report["commands"].get(command, {}).get("success", False)
        for command in REQUIRED_COMMANDS
    )
    any_ok = any(result["success"] for result in report["commands"].values())
    report["overall_result"] = (
        "success" if required_ok else "partial" if any_ok else "failed"
    )

    json_path, txt_path = save_reports(report, args.output_dir)
    print("\nReports saved:")
    print(f"  {json_path}")
    print(f"  {txt_path}")

    if required_ok:
        print("\nRead-only communication with the COSCON was confirmed.")
        return 0

    print(
        "\nThe minimum identity/status check was not completed. "
        "Review the report, Ethernet/subnet settings and Windows firewall."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
