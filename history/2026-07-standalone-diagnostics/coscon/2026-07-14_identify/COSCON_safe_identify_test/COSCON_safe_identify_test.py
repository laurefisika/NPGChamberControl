#!/usr/bin/env python3
"""Safe write-path test for the SPECS COSCON IS UDP interface.

This program can send only five exact commands:
    Info
    GetStatus
    GetIdentify
    SetIdentify on
    SetIdentify off

The only state-changing action is blinking the front-panel "It's me" LED.
It cannot send Operate, Degas, Standby, Off, Reset, network, or preset commands.

Normal test:
    python COSCON_safe_identify_test.py

Emergency LED cleanup only:
    python COSCON_safe_identify_test.py --off-only
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_HOST = "192.168.236.186"
DEFAULT_PORT = 2005
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_RETRIES = 1
DEFAULT_DURATION_S = 10

# Hard safety boundary: no command outside this exact set can reach the socket.
ALLOWED_EXACT_COMMANDS = frozenset(
    {
        "Info",
        "GetStatus",
        "GetIdentify",
        "SetIdentify on",
        "SetIdentify off",
    }
)

FIELD_RE = re.compile(r'\b([A-Za-z][A-Za-z0-9_]*)=(?:"([^"]*)"|([^\s\r\n]+))')


class CosconIdentifyTestError(RuntimeError):
    """Base error for the safe identify test."""


class CosconTimeout(CosconIdentifyTestError):
    """The COSCON did not answer within the configured timeout."""


class CommandBlocked(CosconIdentifyTestError):
    """A command outside the hard-coded safe whitelist was attempted."""


@dataclass(frozen=True)
class Exchange:
    command: str
    reply: str
    ok: bool
    source_host: str
    source_port: int
    elapsed_s: float


def normalize_and_validate(command: str) -> str:
    normalized = " ".join((command or "").strip().split())
    if normalized not in ALLOWED_EXACT_COMMANDS:
        raise CommandBlocked(
            f"Blocked COSCON command {normalized!r}. "
            "This test permits only Info, GetStatus, GetIdentify, "
            "SetIdentify on and SetIdentify off."
        )
    return normalized


def reply_is_ok(reply: str) -> bool:
    upper = (reply or "").upper()
    if not upper.strip():
        return False
    return not any(marker in upper for marker in (" ERROR", "ERROR:", " FAIL", "FAILED"))


def parse_fields(reply: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FIELD_RE.finditer(reply or ""):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        fields[match.group(1)] = value
    return fields


def parse_identify_state(reply: str) -> str | None:
    match = re.search(r"\bGetIdentify(?:\s+OK:?)?\s+(on|off)\b", reply or "", re.IGNORECASE)
    return match.group(1).lower() if match else None


def send_exact_command(
    command: str,
    *,
    host: str,
    port: int,
    timeout_s: float,
    retries: int,
) -> Exchange:
    normalized = normalize_and_validate(command)
    payload = (normalized + "\r").encode("ascii", errors="strict")
    last_error: BaseException | None = None

    for attempt in range(retries + 1):
        started = time.monotonic()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            try:
                sock.sendto(payload, (host, port))
                data, source = sock.recvfrom(8192)
                elapsed = time.monotonic() - started
                reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
                return Exchange(
                    command=normalized,
                    reply=reply,
                    ok=reply_is_ok(reply),
                    source_host=str(source[0]),
                    source_port=int(source[1]),
                    elapsed_s=elapsed,
                )
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
            except OSError as exc:
                raise CosconIdentifyTestError(
                    f"UDP communication failed for {normalized!r}: {exc}"
                ) from exc

    raise CosconTimeout(
        f"No reply from COSCON {host}:{port} for {normalized!r} after "
        f"{retries + 1} attempt(s), timeout {timeout_s:.2f} s."
    ) from last_error


def print_exchange(exchange: Exchange) -> None:
    label = "OK" if exchange.ok else "ERROR"
    print(f"[{label}] {exchange.command}")
    print(f"     {exchange.reply}")


def add_exchange(report: dict[str, Any], exchange: Exchange) -> None:
    report["exchanges"].append(asdict(exchange))
    print_exchange(exchange)
    if not exchange.ok:
        raise CosconIdentifyTestError(
            f"COSCON returned an error/failure reply to {exchange.command!r}."
        )


def require_safe_initial_status(exchange: Exchange) -> None:
    fields = parse_fields(exchange.reply)
    mode = fields.get("Mode", "")
    interlock = fields.get("Interlock", "")
    if mode.casefold() != "off":
        raise CosconIdentifyTestError(
            f"Test blocked: COSCON Mode is {mode or 'unknown'}, not Off. "
            "No write command was sent."
        )
    if interlock.casefold() != "ok":
        raise CosconIdentifyTestError(
            f"Test blocked: COSCON Interlock is {interlock or 'unknown'}, not OK. "
            "No write command was sent."
        )


def save_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"coscon_identify_test_{stamp}.json"
    txt_path = output_dir / f"coscon_identify_test_{stamp}.txt"

    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "COSCON IS SAFE IDENTIFY LED TEST",
        "================================",
        f"Timestamp: {report['timestamp']}",
        f"Target: {report['target']['host']}:{report['target']['port']}",
        f"Result: {report['result']}",
        "",
        "Permitted commands:",
        *[f"  - {command}" for command in sorted(ALLOWED_EXACT_COMMANDS)],
        "",
    ]
    for item in report["exchanges"]:
        lines.append(f"[{ 'OK' if item['ok'] else 'ERROR' }] {item['command']}")
        lines.append(f"    {item['reply']}")
        lines.append("")
    if report.get("error"):
        lines.extend(["Error:", f"  {report['error']}", ""])

    txt_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return json_path, txt_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely verify COSCON UDP write communication by blinking only the "
            "front-panel 'It's me' LED."
        )
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="COSCON IPv4 address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="COSCON UDP port")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_S,
        help="LED blink duration in seconds (2-60, default 10)",
    )
    parser.add_argument(
        "--off-only",
        action="store_true",
        help="Only send SetIdentify off and verify that the LED state is off",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("COSCON Diagnostic Reports"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535.")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive.")
    if args.retries < 0:
        raise SystemExit("--retries must be zero or greater.")
    if not 2 <= args.duration <= 60:
        raise SystemExit("--duration must be between 2 and 60 seconds.")

    report: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": {
            "host": args.host,
            "port": args.port,
            "timeout_s": args.timeout,
            "retries": args.retries,
        },
        "safety": {
            "purpose": "Blink only the COSCON front-panel It's me LED",
            "allowed_exact_commands": sorted(ALLOWED_EXACT_COMMANDS),
            "operate_available": False,
            "degas_available": False,
            "standby_available": False,
            "off_available": False,
            "reset_available": False,
            "configuration_available": False,
        },
        "exchanges": [],
        "result": "pending",
        "error": None,
    }

    print("COSCON IS safe Identify LED test")
    print("================================")
    print(f"Target: {args.host}:{args.port}")
    print("The only write commands available are SetIdentify on/off.")
    print("Operate, Degas, Standby, Off, Reset and configuration are blocked.\n")

    cleanup_needed = False
    try:
        info = send_exact_command(
            "Info", host=args.host, port=args.port,
            timeout_s=args.timeout, retries=args.retries,
        )
        add_exchange(report, info)

        if args.off_only:
            off = send_exact_command(
                "SetIdentify off", host=args.host, port=args.port,
                timeout_s=args.timeout, retries=args.retries,
            )
            add_exchange(report, off)
            verify = send_exact_command(
                "GetIdentify", host=args.host, port=args.port,
                timeout_s=args.timeout, retries=args.retries,
            )
            add_exchange(report, verify)
            if parse_identify_state(verify.reply) != "off":
                raise CosconIdentifyTestError("COSCON did not confirm GetIdentify off.")
            report["result"] = "success_off_only"
            print("\nIdentify LED state was confirmed OFF.")
            return_code = 0
        else:
            status = send_exact_command(
                "GetStatus", host=args.host, port=args.port,
                timeout_s=args.timeout, retries=args.retries,
            )
            add_exchange(report, status)
            require_safe_initial_status(status)

            initial_identify = send_exact_command(
                "GetIdentify", host=args.host, port=args.port,
                timeout_s=args.timeout, retries=args.retries,
            )
            add_exchange(report, initial_identify)

            print("\nPRE-TEST CHECK")
            print("- COSCON has confirmed Mode=Off and Interlock=OK.")
            print("- This test will only blink the front-panel 'It's me' LED.")
            print("- Keep the COSCON front panel visible during the test.")
            confirmation = input("Type IDENTIFY to start the LED test: ").strip()
            if confirmation != "IDENTIFY":
                report["result"] = "cancelled"
                print("\nTest cancelled. No write command was sent.")
                return_code = 2
            else:
                on = send_exact_command(
                    "SetIdentify on", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=args.retries,
                )
                cleanup_needed = True
                add_exchange(report, on)

                verify_on = send_exact_command(
                    "GetIdentify", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=args.retries,
                )
                add_exchange(report, verify_on)
                if parse_identify_state(verify_on.reply) != "on":
                    raise CosconIdentifyTestError("COSCON did not confirm GetIdentify on.")

                print("\nThe 'It's me' LED should now be blinking.")
                for remaining in range(args.duration, 0, -1):
                    print(f"  Turning it off in {remaining:2d} s...", end="\r", flush=True)
                    time.sleep(1)
                print(" " * 48, end="\r")

                off = send_exact_command(
                    "SetIdentify off", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=args.retries,
                )
                add_exchange(report, off)
                cleanup_needed = False

                verify_off = send_exact_command(
                    "GetIdentify", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=args.retries,
                )
                add_exchange(report, verify_off)
                if parse_identify_state(verify_off.reply) != "off":
                    raise CosconIdentifyTestError("COSCON did not confirm GetIdentify off.")

                final_status = send_exact_command(
                    "GetStatus", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=args.retries,
                )
                add_exchange(report, final_status)
                require_safe_initial_status(final_status)

                report["result"] = "success"
                print("\nSUCCESS: UDP write communication was verified.")
                print("The Identify LED was turned ON, confirmed, turned OFF, and confirmed OFF.")
                print("COSCON remained in Mode=Off with Interlock=OK.")
                return_code = 0

    except KeyboardInterrupt:
        report["result"] = "interrupted"
        report["error"] = "KeyboardInterrupt"
        print("\n\nTest interrupted. Attempting to turn the Identify LED off...")
        return_code = 130
    except Exception as exc:  # noqa: BLE001 - diagnostic records the exact failure
        report["result"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"\nERROR: {exc}")
        return_code = 1
    finally:
        if cleanup_needed:
            try:
                cleanup = send_exact_command(
                    "SetIdentify off", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=max(args.retries, 2),
                )
                report["exchanges"].append(asdict(cleanup))
                print_exchange(cleanup)
                verify_cleanup = send_exact_command(
                    "GetIdentify", host=args.host, port=args.port,
                    timeout_s=args.timeout, retries=max(args.retries, 2),
                )
                report["exchanges"].append(asdict(verify_cleanup))
                print_exchange(verify_cleanup)
            except Exception as cleanup_exc:  # noqa: BLE001
                cleanup_text = f"Cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}"
                report["error"] = (
                    f"{report['error']}; {cleanup_text}" if report.get("error") else cleanup_text
                )
                print(f"WARNING: {cleanup_text}")
                print("Run this program again with --off-only to stop the Identify LED blinking.")

        json_path, txt_path = save_report(report, args.output_dir)
        print("\nReport saved:")
        print(f"  {json_path}")
        print(f"  {txt_path}")

    return return_code


if __name__ == "__main__":
    sys.exit(main())
