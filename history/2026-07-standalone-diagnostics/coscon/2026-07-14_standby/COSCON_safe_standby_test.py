#!/usr/bin/env python3
"""
COSCON IS supervised safety test: Off -> Standby -> Off

This script is intentionally limited to:
    Info
    GetStatus
    GetMonitorValues
    GetDiagnosticValues
    GetTargetValues
    SwitchToStandby
    SwitchToOff

It cannot send Degas, Operate, Reset, network, or preset-write commands.

Protocol:
- UDP
- Port 2005
- ASCII commands terminated by carriage return (CR)
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_IP = "192.168.236.186"
DEFAULT_PORT = 2005
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_TRANSITION_TIMEOUT_S = 30.0
DEFAULT_HOLD_S = 10.0
POLL_PERIOD_S = 0.5

READ_ONLY_COMMANDS = {
    "Info",
    "GetStatus",
    "GetMonitorValues",
    "GetDiagnosticValues",
    "GetTargetValues",
}
WRITE_COMMANDS = {
    "SwitchToStandby",
    "SwitchToOff",
}
ALLOWED_COMMANDS = READ_ONLY_COMMANDS | WRITE_COMMANDS

CONFIRMATION_PHRASE = "STANDBY TEST"


@dataclass
class Status:
    mode: str
    interlock: str
    details: str
    raw: str


class CosconError(RuntimeError):
    pass


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, message: str = "") -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{timestamp}] {message}" if message else ""
        self.lines.append(line)
        print(line)

    def save(self, target_dir: Path, result: str, metadata: dict) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = target_dir / f"coscon_standby_test_{stamp}.txt"
        header = [
            "COSCON IS SUPERVISED STANDBY TEST",
            "===================================",
            f"Timestamp: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"Result: {result}",
            f"Target: {metadata['ip']}:{metadata['port']}",
            f"Hold time: {metadata['hold_s']} s",
            "",
            "Permitted commands:",
        ]
        header.extend(f"  - {cmd}" for cmd in sorted(ALLOWED_COMMANDS))
        header.extend(["", "LOG", "---"])
        path.write_text("\n".join(header + self.lines) + "\n", encoding="utf-8")
        return path


class CosconUDP:
    def __init__(self, ip: str, port: int, timeout_s: float, logger: Logger) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self.logger = logger

    @staticmethod
    def _validate_command(command: str) -> None:
        if command not in ALLOWED_COMMANDS:
            raise CosconError(f"Blocked command: {command!r}")

    def send(self, command: str) -> str:
        self._validate_command(command)
        payload = (command + "\r").encode("ascii")
        self.logger.add(f"-> {command}")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendto(payload, (self.ip, self.port))
            try:
                data, sender = sock.recvfrom(4096)
            except socket.timeout as exc:
                raise CosconError(f"No reply to {command!r} within {self.timeout_s:.1f} s.") from exc

        if sender[0] != self.ip:
            raise CosconError(
                f"Reply to {command!r} came from unexpected address {sender[0]}:{sender[1]}."
            )

        reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
        self.logger.add(f"<- {reply}")
        if not reply:
            raise CosconError(f"Empty reply to {command!r}.")
        if "ERROR" in reply.upper() or "FAIL" in reply.upper():
            raise CosconError(f"COSCON rejected {command!r}: {reply}")
        return reply


STATUS_RE = re.compile(
    r"Mode=(?P<mode>[^\s]+)\s+"
    r"Interlock=(?P<interlock>[^\s]+)"
    r"(?:\s+Details=\"(?P<details>.*)\")?",
    re.IGNORECASE,
)


def parse_status(reply: str) -> Status:
    match = STATUS_RE.search(reply)
    if not match:
        raise CosconError(f"Could not parse GetStatus reply: {reply!r}")
    return Status(
        mode=match.group("mode"),
        interlock=match.group("interlock"),
        details=match.group("details") or "",
        raw=reply,
    )


def get_status(client: CosconUDP) -> Status:
    return parse_status(client.send("GetStatus"))


def wait_for_mode(
    client: CosconUDP,
    target_mode: str,
    timeout_s: float,
    *,
    require_interlock_ok: bool = True,
) -> Status:
    deadline = time.monotonic() + timeout_s
    last_status: Optional[Status] = None

    while time.monotonic() < deadline:
        status = get_status(client)
        last_status = status

        if require_interlock_ok and status.interlock.lower() != "ok":
            raise CosconError(
                f"Interlock is not OK while waiting for {target_mode}: "
                f"{status.interlock} ({status.details})"
            )

        if status.mode.lower() == "error":
            raise CosconError(f"COSCON entered Error mode: {status.details}")

        if status.mode.lower() == target_mode.lower():
            return status

        time.sleep(POLL_PERIOD_S)

    raise CosconError(
        f"Timeout waiting for Mode={target_mode}. "
        f"Last status: {last_status.raw if last_status else 'no status received'}"
    )


def require_initial_safe_state(client: CosconUDP) -> Status:
    status = get_status(client)
    if status.mode.lower() != "off":
        raise CosconError(
            f"Test refused: initial Mode must be Off, but COSCON reports {status.mode}."
        )
    if status.interlock.lower() != "ok":
        raise CosconError(
            f"Test refused: initial Interlock must be OK, but COSCON reports "
            f"{status.interlock} ({status.details})."
        )
    return status


def print_preflight() -> None:
    print(
        "\nPRE-FLIGHT CHECK — confirm physically before continuing\n"
        "------------------------------------------------------\n"
        "1. The IQE 11/35 source and COSCON cable are correctly connected.\n"
        "2. The chamber is under the vacuum conditions required by your SOP.\n"
        "3. The COSCON front panel shows no fault or interlock alarm.\n"
        "4. No other program/operator will issue COSCON commands during the test.\n"
        "5. A trained operator is present and can switch the equipment off locally.\n"
        "6. The argon leak valve remains closed for this Standby test.\n"
        "\n"
        "This test briefly warms the filament in Standby, keeps beam energy at zero,\n"
        "then returns the COSCON to Off.\n"
    )


def run_test(args: argparse.Namespace) -> int:
    logger = Logger()
    client = CosconUDP(args.ip, args.port, args.timeout, logger)
    reports_dir = Path(args.report_dir)
    result = "failure"
    standby_requested = False
    off_confirmed = False
    metadata = {"ip": args.ip, "port": args.port, "hold_s": args.hold}

    try:
        logger.add("Starting COSCON supervised Off -> Standby -> Off test.")
        client.send("Info")
        initial = require_initial_safe_state(client)
        logger.add(
            f"Initial state accepted: Mode={initial.mode}, "
            f"Interlock={initial.interlock}, Details={initial.details!r}"
        )
        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        if args.off_only:
            logger.add("OFF-ONLY mode selected.")
            client.send("SwitchToOff")
            wait_for_mode(client, "Off", args.transition_timeout, require_interlock_ok=False)
            off_confirmed = True
            result = "success"
            logger.add("COSCON confirmed Mode=Off.")
            return 0

        print_preflight()
        phrase = input(
            f'Type exactly "{CONFIRMATION_PHRASE}" to begin, or press Enter to cancel:\n> '
        ).strip()
        if phrase != CONFIRMATION_PHRASE:
            logger.add("Test cancelled before any state-changing command was sent.")
            result = "cancelled"
            return 2

        # Re-check immediately before the first state-changing command.
        require_initial_safe_state(client)

        logger.add("Requesting Standby.")
        reply = client.send("SwitchToStandby")
        if "OK" not in reply.upper():
            raise CosconError(f"Unexpected SwitchToStandby reply: {reply}")
        standby_requested = True

        standby = wait_for_mode(client, "Standby", args.transition_timeout)
        logger.add(
            f"Standby confirmed: Mode={standby.mode}, "
            f"Interlock={standby.interlock}, Details={standby.details!r}"
        )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        logger.add(f"Holding Standby for {args.hold:.1f} s.")
        hold_deadline = time.monotonic() + args.hold
        while time.monotonic() < hold_deadline:
            status = get_status(client)
            if status.interlock.lower() != "ok":
                raise CosconError(
                    f"Interlock changed during Standby: {status.interlock} "
                    f"({status.details})"
                )
            if status.mode.lower() == "error":
                raise CosconError(f"COSCON entered Error mode: {status.details}")
            if status.mode.lower() != "standby":
                raise CosconError(
                    f"Unexpected mode during Standby hold: {status.mode} ({status.details})"
                )
            time.sleep(min(1.0, max(0.0, hold_deadline - time.monotonic())))

        logger.add("Requesting Off.")
        reply = client.send("SwitchToOff")
        if "OK" not in reply.upper():
            raise CosconError(f"Unexpected SwitchToOff reply: {reply}")

        final = wait_for_mode(
            client,
            "Off",
            args.transition_timeout,
            require_interlock_ok=False,
        )
        off_confirmed = True
        logger.add(
            f"Final Off confirmed: Mode={final.mode}, "
            f"Interlock={final.interlock}, Details={final.details!r}"
        )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        if final.interlock.lower() != "ok":
            raise CosconError(
                f"COSCON reached Off but Interlock is not OK: "
                f"{final.interlock} ({final.details})"
            )

        result = "success"
        logger.add("SUCCESS: Off -> Standby -> Off was completed and verified.")
        return 0

    except KeyboardInterrupt:
        logger.add("Keyboard interrupt received.")
        result = "interrupted"
        return 130

    except Exception as exc:
        logger.add(f"ERROR: {exc}")
        result = "failure"
        return 1

    finally:
        # Best-effort safe return. This is only attempted if the test had begun
        # and Mode=Off has not already been confirmed.
        if standby_requested and not off_confirmed:
            logger.add("Failsafe: attempting SwitchToOff.")
            try:
                client.send("SwitchToOff")
                final = wait_for_mode(
                    client,
                    "Off",
                    args.transition_timeout,
                    require_interlock_ok=False,
                )
                off_confirmed = True
                logger.add(
                    f"Failsafe Off confirmed: Mode={final.mode}, "
                    f"Interlock={final.interlock}, Details={final.details!r}"
                )
            except Exception as cleanup_exc:
                logger.add(
                    "CRITICAL: automatic return to Off could not be confirmed: "
                    f"{cleanup_exc}. Use the local COSCON controls and contact a trained operator."
                )

        report_path = logger.save(reports_dir, result, metadata)
        print(f"\nReport saved:\n  {report_path.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supervised COSCON IS Off -> Standby -> Off UDP test."
    )
    parser.add_argument("--ip", default=DEFAULT_IP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--transition-timeout",
        type=float,
        default=DEFAULT_TRANSITION_TIMEOUT_S,
    )
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD_S)
    parser.add_argument(
        "--report-dir",
        default="COSCON Diagnostic Reports",
    )
    parser.add_argument(
        "--off-only",
        action="store_true",
        help="Only request and verify Mode=Off; does not enter Standby.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.hold < 0:
        raise SystemExit("--hold must be zero or positive.")
    return run_test(args)


if __name__ == "__main__":
    sys.exit(main())
