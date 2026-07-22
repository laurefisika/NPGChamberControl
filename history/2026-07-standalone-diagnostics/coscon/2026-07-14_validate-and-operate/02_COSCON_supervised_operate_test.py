#!/usr/bin/env python3
"""
Supervised active COSCON operating test.

Default active target:
    Emission = 0.010 A (10 mA)
    Energy   = 2250 V

Default sequence:
    Off
    -> ValidateOperateTarget
    -> SwitchToOperate
    -> verify Operating
    -> hold 5 seconds with pressure/interlock monitoring
    -> SwitchToStandby
    -> verify Standby
    -> SwitchToOff
    -> verify Off

The argon leak valve remains manual.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

from coscon_test_common import (
    CosconUDP,
    PressureSafetyError,
    ReportLogger,
    TestError,
    XGS600,
    get_status,
    read_pressure_in_window,
    require_interlock_ok,
    safe_stop_operating_source,
    wait_for_modes,
)

DEFAULT_IP = "192.168.236.186"
DEFAULT_PORT = 2005
DEFAULT_TIMEOUT_S = 2.0

DEFAULT_XGS_PORT = "COM6"
DEFAULT_XGS_BAUD = 9600
DEFAULT_XGS_TIMEOUT_S = 1.0

DEFAULT_EMISSION_A = 0.010
DEFAULT_ENERGY_V = 2250.0

DEFAULT_PRESSURE_MIN_MBAR = 1.0e-5
DEFAULT_PRESSURE_MAX_MBAR = 5.0e-5
DEFAULT_HOLD_S = 5.0
DEFAULT_TRANSITION_TIMEOUT_S = 30.0
DEFAULT_POLL_S = 0.75

CONFIRMATION = "OPERATE 10mA 2250V"

EXACT_COMMANDS = {
    "Info",
    "GetStatus",
    "GetTargetValues",
    "GetMonitorValues",
    "GetDiagnosticValues",
    "SwitchToStandby",
    "SwitchToOff",
}
VALIDATE_RE = re.compile(
    r"^ValidateOperateTarget Emission="
    r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)? "
    r"Energy=[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$"
)
OPERATE_RE = re.compile(
    r"^SwitchToOperate Emission="
    r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)? "
    r"Energy=[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$"
)


def command_allowed(command: str) -> bool:
    return (
        command in EXACT_COMMANDS
        or bool(VALIDATE_RE.fullmatch(command))
        or bool(OPERATE_RE.fullmatch(command))
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supervised active COSCON Operating -> Standby -> Off test."
    )
    parser.add_argument("--ip", default=DEFAULT_IP)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)

    parser.add_argument("--xgs-port", default=DEFAULT_XGS_PORT)
    parser.add_argument("--xgs-baud", type=int, default=DEFAULT_XGS_BAUD)
    parser.add_argument("--xgs-timeout", type=float, default=DEFAULT_XGS_TIMEOUT_S)

    parser.add_argument("--emission", type=float, default=DEFAULT_EMISSION_A)
    parser.add_argument("--energy", type=float, default=DEFAULT_ENERGY_V)
    parser.add_argument("--pressure-min", type=float, default=DEFAULT_PRESSURE_MIN_MBAR)
    parser.add_argument("--pressure-max", type=float, default=DEFAULT_PRESSURE_MAX_MBAR)
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD_S)
    parser.add_argument(
        "--transition-timeout",
        type=float,
        default=DEFAULT_TRANSITION_TIMEOUT_S,
    )
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S)
    parser.add_argument("--report-dir", default="COSCON Diagnostic Reports")
    parser.add_argument(
        "--safe-stop-only",
        action="store_true",
        help="Do not activate the source; only request Standby then Off.",
    )
    return parser


def preflight_message(args: argparse.Namespace) -> None:
    print(
        "\nACTIVE HIGH-VOLTAGE TEST — PHYSICAL PRE-FLIGHT\n"
        "----------------------------------------------\n"
        "1. A trained operator is physically present.\n"
        "2. A complete Degas cycle has been performed and COSCON returned to Off.\n"
        "3. Sputter-gun cable orientation and connection are confirmed.\n"
        "4. The manual argon valve is open and pressure is stabilized near 2e-5 mbar.\n"
        "5. The sample/shutter configuration is safe for a brief sputtering pulse.\n"
        "6. COSCON shows no fault and the physical controls are accessible.\n"
        "7. SpecsLab, the web UI and Phase 2 will not issue COSCON commands.\n"
        "8. No other program is using the XGS600 COM port.\n"
        "\n"
        f"Target: {args.emission * 1000:.3f} mA, {args.energy:.1f} V\n"
        f"Allowed pressure window: {args.pressure_min:.1e} to "
        f"{args.pressure_max:.1e} mbar\n"
        f"Operating hold: {args.hold:.1f} seconds\n"
    )


def main() -> int:
    args = build_parser().parse_args()
    logger = ReportLogger()
    result = "failure"
    reason = "Active test did not complete."
    final_off_confirmed = False
    activation_requested = False
    gauge: Optional[XGS600] = None

    metadata = {
        "COSCON target": f"{args.ip}:{args.port}",
        "Pressure gauge": f"{args.xgs_port} at {args.xgs_baud} baud",
        "Requested emission": f"{args.emission:.6e} A",
        "Requested energy": f"{args.energy:.1f} V",
        "Allowed pressure window": (
            f"{args.pressure_min:.6e} to {args.pressure_max:.6e} mbar"
        ),
        "Operating hold": f"{args.hold:.1f} s",
        "Final Mode=Off confirmed": lambda: final_off_confirmed,
    }

    client = CosconUDP(
        args.ip,
        args.port,
        args.timeout,
        logger,
        command_allowed,
    )

    try:
        if args.emission <= 0 or args.energy <= 0:
            raise TestError("Emission and energy must both be positive.")
        if args.pressure_min <= 0 or args.pressure_max <= args.pressure_min:
            raise TestError("Invalid pressure window.")
        if args.hold < 0:
            raise TestError("Hold time cannot be negative.")

        logger.add("Starting supervised active COSCON operating test.")
        client.send("Info")

        if args.safe_stop_only:
            final_off_confirmed = safe_stop_operating_source(
                client,
                logger,
                standby_timeout_s=args.transition_timeout,
                off_timeout_s=args.transition_timeout,
                poll_s=args.poll,
            )
            if not final_off_confirmed:
                raise TestError("Safe-stop helper could not confirm Mode=Off.")
            result = "success"
            reason = "Safe-stop-only helper confirmed Mode=Off."
            logger.add("SUCCESS: " + reason)
            return 0

        status = get_status(client)
        require_interlock_ok(status, "before active test")
        if status.mode.lower() != "off":
            raise TestError(
                f"Test requires initial Mode=Off, but COSCON reports {status.mode}."
            )

        gauge = XGS600(
            args.xgs_port,
            args.xgs_baud,
            args.xgs_timeout,
            logger,
        )

        # Three stable, valid preflight readings.
        for index in range(3):
            read_pressure_in_window(
                gauge,
                logger,
                min_mbar=args.pressure_min,
                max_mbar=args.pressure_max,
                context=f"preflight {index + 1}/3",
            )
            if index < 2:
                time.sleep(0.5)

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        validate_command = (
            f"ValidateOperateTarget Emission={args.emission:.6e} "
            f"Energy={args.energy:.6g}"
        )
        validate_reply = client.send(validate_command)
        if "OK" not in validate_reply.upper():
            raise TestError(f"Unexpected validation reply: {validate_reply}")

        preflight_message(args)
        typed = input(
            f'Type exactly "{CONFIRMATION}" to activate the source, '
            "or press Enter to cancel:\n> "
        ).strip()
        if typed != CONFIRMATION:
            result = "cancelled"
            reason = "Cancelled before SwitchToOperate."
            logger.add(reason)
            return 2

        # Last checks immediately before high voltage.
        status = get_status(client)
        require_interlock_ok(status, "immediately before SwitchToOperate")
        if status.mode.lower() != "off":
            raise TestError(
                f"Mode changed to {status.mode}; activation cancelled."
            )

        for index in range(2):
            read_pressure_in_window(
                gauge,
                logger,
                min_mbar=args.pressure_min,
                max_mbar=args.pressure_max,
                context=f"final pre-Operate {index + 1}/2",
            )
            if index == 0:
                time.sleep(0.4)

        operate_command = (
            f"SwitchToOperate Emission={args.emission:.6e} "
            f"Energy={args.energy:.6g}"
        )
        logger.add("Requesting Operating mode.")
        activation_requested = True
        try:
            reply = client.send(operate_command)
            if "OK" not in reply.upper():
                raise TestError(f"Unexpected SwitchToOperate reply: {reply}")
        except TestError as exc:
            # UDP reply loss does not prove the command was not received.
            logger.add(
                f"SwitchToOperate reply problem: {exc}. "
                "Polling state before deciding."
            )

        operating = wait_for_modes(
            client,
            {"Operating"},
            args.transition_timeout,
            args.poll,
            require_ok_interlock=True,
            allowed_transient_modes={
                "off",
                "switchingtooperate",
                "operating",
                "error",
            },
        )
        logger.add(
            f"Operating confirmed: Interlock={operating.interlock}, "
            f"Details={operating.details!r}"
        )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        logger.add(
            f"Holding Operating for {args.hold:.1f} seconds with active "
            "pressure/interlock monitoring."
        )
        deadline = time.monotonic() + args.hold
        while time.monotonic() < deadline:
            status = get_status(client)
            require_interlock_ok(status, "during Operating hold")
            if status.mode.lower() != "operating":
                raise TestError(
                    f"Unexpected mode during Operating hold: "
                    f"{status.mode} ({status.details})"
                )

            read_pressure_in_window(
                gauge,
                logger,
                min_mbar=args.pressure_min,
                max_mbar=args.pressure_max,
                context="Operating hold",
            )
            time.sleep(min(args.poll, max(0.0, deadline - time.monotonic())))

        logger.add("Requesting Standby.")
        try:
            reply = client.send("SwitchToStandby")
            if "OK" not in reply.upper():
                raise TestError(f"Unexpected SwitchToStandby reply: {reply}")
        except TestError as exc:
            logger.add(
                f"SwitchToStandby reply problem: {exc}. "
                "Polling state before deciding."
            )

        standby = wait_for_modes(
            client,
            {"Standby"},
            args.transition_timeout,
            args.poll,
            require_ok_interlock=True,
            allowed_transient_modes={
                "operating",
                "switchingtostandby",
                "standby",
                "error",
            },
        )
        logger.add(
            f"Standby confirmed: Interlock={standby.interlock}, "
            f"Details={standby.details!r}"
        )

        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        logger.add("Requesting Off.")
        try:
            reply = client.send("SwitchToOff")
            if "OK" not in reply.upper():
                raise TestError(f"Unexpected SwitchToOff reply: {reply}")
        except TestError as exc:
            logger.add(
                f"SwitchToOff reply problem: {exc}. Polling state before deciding."
            )

        final = wait_for_modes(
            client,
            {"Off"},
            args.transition_timeout,
            args.poll,
            require_ok_interlock=False,
            allowed_transient_modes={
                "standby",
                "switchingtooff",
                "off",
                "error",
            },
        )
        final_off_confirmed = True
        if final.interlock.lower() != "ok":
            raise TestError(
                f"Mode=Off reached but Interlock={final.interlock}: "
                f"{final.details}"
            )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        result = "success"
        reason = (
            "Operating was confirmed, Operating->Standby was confirmed, "
            "and final Mode=Off was confirmed."
        )
        logger.add("SUCCESS: " + reason)
        return 0

    except KeyboardInterrupt:
        result = "interrupted"
        reason = "Keyboard interrupt received."
        logger.add(reason)
        return 130

    except PressureSafetyError as exc:
        result = "pressure_safety_stop"
        reason = str(exc)
        logger.add("PRESSURE SAFETY STOP: " + reason)
        return 3

    except Exception as exc:
        result = "failure"
        reason = str(exc)
        logger.add("ERROR: " + reason)
        return 1

    finally:
        if activation_requested and not final_off_confirmed:
            logger.add("Failsafe path activated.")
            final_off_confirmed = safe_stop_operating_source(
                client,
                logger,
                standby_timeout_s=args.transition_timeout,
                off_timeout_s=args.transition_timeout,
                poll_s=args.poll,
            )

        if gauge is not None:
            gauge.close()

        evaluated_metadata = {}
        for key, value in metadata.items():
            evaluated_metadata[key] = value() if callable(value) else value

        report = logger.save(
            Path(args.report_dir),
            filename_prefix="coscon_operate_test",
            title="COSCON IS SUPERVISED ACTIVE OPERATING TEST",
            result=result,
            reason=reason,
            metadata=evaluated_metadata,
            permitted_commands=[
                *sorted(EXACT_COMMANDS),
                "ValidateOperateTarget Emission=<number> Energy=<number>",
                "SwitchToOperate Emission=<number> Energy=<number>",
            ],
        )
        print(f"\nReport saved:\n  {report.resolve()}")


if __name__ == "__main__":
    sys.exit(main())
