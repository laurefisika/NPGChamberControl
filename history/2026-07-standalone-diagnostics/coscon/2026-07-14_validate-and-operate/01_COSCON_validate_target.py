#!/usr/bin/env python3
"""
Read-only validation of a COSCON operating target.

Default target:
    Emission = 0.010 A (10 mA)
    Energy   = 2250 V

This script cannot activate the ion source.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from coscon_test_common import (
    CosconUDP,
    ReportLogger,
    TestError,
    get_status,
    require_interlock_ok,
)

DEFAULT_IP = "192.168.236.186"
DEFAULT_PORT = 2005
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_EMISSION_A = 0.010
DEFAULT_ENERGY_V = 2250.0
CONFIRMATION = "VALIDATE TARGET"

EXACT_READ_COMMANDS = {
    "Info",
    "GetStatus",
    "GetTargetValues",
    "GetMonitorValues",
    "GetDiagnosticValues",
}
VALIDATE_RE = re.compile(
    r"^ValidateOperateTarget Emission="
    r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)? "
    r"Energy=[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$"
)


def command_allowed(command: str) -> bool:
    return command in EXACT_READ_COMMANDS or bool(VALIDATE_RE.fullmatch(command))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only COSCON ValidateOperateTarget test."
    )
    parser.add_argument("--ip", default=DEFAULT_IP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--emission", type=float, default=DEFAULT_EMISSION_A)
    parser.add_argument("--energy", type=float, default=DEFAULT_ENERGY_V)
    parser.add_argument("--report-dir", default="COSCON Diagnostic Reports")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logger = ReportLogger()
    result = "failure"
    reason = "Validation did not complete."

    metadata = {
        "COSCON target": f"{args.ip}:{args.port}",
        "Requested emission": f"{args.emission:.6e} A",
        "Requested energy": f"{args.energy:.1f} V",
        "State-changing commands available": "No",
    }

    client = CosconUDP(
        args.ip,
        args.port,
        args.timeout,
        logger,
        command_allowed,
    )

    try:
        if args.emission <= 0 or args.energy < 0:
            raise TestError("Emission must be positive and energy cannot be negative.")

        logger.add("Starting read-only operating-target validation.")
        client.send("Info")
        status = get_status(client)
        require_interlock_ok(status, "before validation")

        if status.mode.lower() not in {"off", "standby"}:
            raise TestError(
                f"Validation refused in Mode={status.mode}; expected Off or Standby."
            )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        print(
            "\nThis test does NOT activate the source.\n"
            f"It will validate Emission={args.emission:.6f} A "
            f"and Energy={args.energy:.1f} V.\n"
        )
        typed = input(
            f'Type exactly "{CONFIRMATION}" to continue, '
            "or press Enter to cancel:\n> "
        ).strip()
        if typed != CONFIRMATION:
            result = "cancelled"
            reason = "Cancelled before ValidateOperateTarget."
            logger.add(reason)
            return 2

        # Recheck just before validation.
        status = get_status(client)
        require_interlock_ok(status, "immediately before validation")
        if status.mode.lower() not in {"off", "standby"}:
            raise TestError(
                f"Mode changed to {status.mode}; validation cancelled."
            )

        command = (
            f"ValidateOperateTarget Emission={args.emission:.6e} "
            f"Energy={args.energy:.6g}"
        )
        reply = client.send(command)
        if "OK" not in reply.upper():
            raise TestError(f"Unexpected validation reply: {reply}")

        final_status = get_status(client)
        if final_status.mode.lower() != status.mode.lower():
            raise TestError(
                f"Unexpected state change after read-only validation: "
                f"{status.mode} -> {final_status.mode}"
            )

        result = "success"
        reason = (
            "ValidateOperateTarget returned OK and COSCON state did not change."
        )
        logger.add("SUCCESS: " + reason)
        return 0

    except Exception as exc:
        reason = str(exc)
        logger.add("ERROR: " + reason)
        return 1

    finally:
        report = logger.save(
            Path(args.report_dir),
            filename_prefix="coscon_validate_target",
            title="COSCON IS READ-ONLY OPERATING-TARGET VALIDATION",
            result=result,
            reason=reason,
            metadata=metadata,
            permitted_commands=[
                *sorted(EXACT_READ_COMMANDS),
                "ValidateOperateTarget Emission=<number> Energy=<number>",
            ],
        )
        print(f"\nReport saved:\n  {report.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
