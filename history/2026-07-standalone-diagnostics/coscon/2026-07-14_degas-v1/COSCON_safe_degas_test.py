#!/usr/bin/env python3
"""
COSCON IS supervised brief Degas test
=====================================

Default test sequence:
    Off -> Degas -> observe for 10 s -> Standby -> Off

The script also monitors the synthesis-chamber pressure through the
XGS600 on COM6. It refuses to start if pressure cannot be read or is
above the configured start limit. During Degas it requests Standby
immediately if pressure reaches the documented 1e-4 mbar cutoff, then
requests Off and verifies the final state.

This is a brief transition test, not a complete degassing procedure.

Strictly permitted COSCON commands:
    Info
    GetStatus
    GetMonitorValues
    GetDiagnosticValues
    GetTargetValues
    SwitchToDegas
    SwitchToStandby
    SwitchToOff

Protocol:
    UDP, ASCII, port 2005, commands terminated with CR.
"""

from __future__ import annotations

import argparse
import math
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError as exc:
    raise SystemExit(
        "pyserial is required. Run this test from the project environment "
        "or install it with: python -m pip install pyserial"
    ) from exc


DEFAULT_IP = "192.168.236.186"
DEFAULT_UDP_PORT = 2005
DEFAULT_UDP_TIMEOUT_S = 2.0

DEFAULT_XGS_PORT = "COM6"
DEFAULT_XGS_BAUD = 9600
DEFAULT_XGS_TIMEOUT_S = 1.0

# The current IQE 11/35 manual states that normal source operation requires
# typical chamber pressure below 1e-5 mbar and that Degas must be interrupted
# if pressure rises above 1e-4 mbar.
DEFAULT_START_PRESSURE_MAX_MBAR = 1.0e-5
DEFAULT_ABORT_PRESSURE_MBAR = 1.0e-4

DEFAULT_DEGAS_START_TIMEOUT_S = 30.0
DEFAULT_STANDBY_TIMEOUT_S = 20.0
DEFAULT_OFF_TIMEOUT_S = 30.0
DEFAULT_OBSERVE_S = 10.0
DEFAULT_POLL_S = 0.75

CONFIRMATION_PHRASE = "DEGAS TEST"

READ_COMMANDS = {
    "Info",
    "GetStatus",
    "GetMonitorValues",
    "GetDiagnosticValues",
    "GetTargetValues",
}
WRITE_COMMANDS = {
    "SwitchToDegas",
    "SwitchToStandby",
    "SwitchToOff",
}
ALLOWED_COMMANDS = READ_COMMANDS | WRITE_COMMANDS


class TestError(RuntimeError):
    pass


class PressureSafetyError(TestError):
    pass


@dataclass
class CosconStatus:
    mode: str
    interlock: str
    details: str
    raw: str


class ReportLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.pressures: list[float] = []

    def add(self, message: str = "") -> None:
        if message:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            line = f"[{stamp}] {message}"
        else:
            line = ""
        self.lines.append(line)
        print(line)

    def add_pressure(self, pressure_mbar: float, context: str) -> None:
        self.pressures.append(pressure_mbar)
        self.add(f"PRESSURE [{context}]: {pressure_mbar:.6e} mbar")

    def save(
        self,
        report_dir: Path,
        *,
        result: str,
        reason: str,
        args: argparse.Namespace,
        final_off_confirmed: bool,
    ) -> Path:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = report_dir / f"coscon_degas_test_{stamp}.txt"

        p_min = min(self.pressures) if self.pressures else None
        p_max = max(self.pressures) if self.pressures else None

        header = [
            "COSCON IS SUPERVISED BRIEF DEGAS TEST",
            "=======================================",
            f"Timestamp: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"Result: {result}",
            f"Reason: {reason}",
            f"COSCON target: {args.ip}:{args.udp_port}",
            f"Pressure gauge: {args.xgs_port} at {args.xgs_baud} baud",
            f"Start pressure maximum: {args.start_pressure_max:.6e} mbar",
            f"Degas abort pressure: {args.abort_pressure:.6e} mbar",
            f"Planned Degas observation: {args.observe_seconds:.1f} s",
            f"Final Mode=Off confirmed: {final_off_confirmed}",
            f"Minimum recorded pressure: {p_min:.6e} mbar" if p_min is not None else "Minimum recorded pressure: unavailable",
            f"Maximum recorded pressure: {p_max:.6e} mbar" if p_max is not None else "Maximum recorded pressure: unavailable",
            "",
            "Permitted COSCON commands:",
        ]
        header.extend(f"  - {command}" for command in sorted(ALLOWED_COMMANDS))
        header.extend(["", "LOG", "---"])

        path.write_text("\n".join(header + self.lines) + "\n", encoding="utf-8")
        return path


class CosconUDP:
    def __init__(
        self,
        ip: str,
        port: int,
        timeout_s: float,
        logger: ReportLogger,
    ) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self.logger = logger

    @staticmethod
    def _validate(command: str) -> None:
        if command not in ALLOWED_COMMANDS:
            raise TestError(f"Blocked COSCON command: {command!r}")

    def send(self, command: str) -> str:
        self._validate(command)
        self.logger.add(f"-> {command}")
        payload = (command + "\r").encode("ascii")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendto(payload, (self.ip, self.port))
            try:
                data, sender = sock.recvfrom(4096)
            except socket.timeout as exc:
                raise TestError(
                    f"No COSCON reply to {command!r} within {self.timeout_s:.1f} s."
                ) from exc

        if sender[0] != self.ip:
            raise TestError(
                f"Reply to {command!r} came from unexpected address "
                f"{sender[0]}:{sender[1]}."
            )

        reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
        self.logger.add(f"<- {reply}")

        if not reply:
            raise TestError(f"Empty COSCON reply to {command!r}.")
        if "ERROR" in reply.upper() or "FAIL" in reply.upper():
            raise TestError(f"COSCON rejected {command!r}: {reply}")
        return reply


class XGS600Pressure:
    COMMAND = b"#0002USYNTH\r"
    NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

    def __init__(
        self,
        port: str,
        baud: int,
        timeout_s: float,
        logger: ReportLogger,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.logger = logger
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=timeout_s,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as exc:
            raise TestError(
                f"Could not open pressure gauge on {port}: {exc}. "
                "Close Phase 2 and any other program using COM6."
            ) from exc

    def close(self) -> None:
        try:
            if self.ser.is_open:
                try:
                    self.ser.reset_input_buffer()
                except Exception:
                    pass
                try:
                    self.ser.reset_output_buffer()
                except Exception:
                    pass
                self.ser.close()
        except Exception as exc:
            self.logger.add(f"Pressure-port cleanup warning: {exc}")

    def read_mbar(self) -> float:
        try:
            self.ser.reset_input_buffer()
            self.ser.write(self.COMMAND)
            self.ser.flush()
            time.sleep(0.12)

            deadline = time.monotonic() + self.timeout_s
            buffer = bytearray()
            last_data_at: Optional[float] = None

            while time.monotonic() < deadline:
                waiting = self.ser.in_waiting
                if waiting:
                    buffer.extend(self.ser.read(waiting))
                    last_data_at = time.monotonic()
                elif buffer and last_data_at is not None and time.monotonic() - last_data_at >= 0.08:
                    break
                time.sleep(0.02)

            if not buffer:
                # One final blocking read attempt, respecting the serial timeout.
                buffer.extend(self.ser.read(100))

            text = bytes(buffer).decode("ascii", errors="ignore").strip()
        except Exception as exc:
            raise PressureSafetyError(f"XGS600 communication failed: {exc}") from exc

        cleaned = text.lstrip(">").strip()
        if cleaned.lower() in {"nan", "+nan", "-nan", ""}:
            raise PressureSafetyError(
                f"Pressure monitoring unavailable: XGS600 returned {text!r}."
            )

        match = self.NUMBER_RE.search(cleaned)
        if not match:
            raise PressureSafetyError(
                f"Could not parse pressure from XGS600 reply {text!r}."
            )

        try:
            value = float(match.group(0))
        except ValueError as exc:
            raise PressureSafetyError(
                f"Invalid pressure value in XGS600 reply {text!r}."
            ) from exc

        if not math.isfinite(value) or value <= 0:
            raise PressureSafetyError(
                f"Unsafe/unusable pressure value from XGS600: {value!r}."
            )
        return value


MODE_RE = re.compile(r"\bMode=(?P<mode>[^\s]+)", re.IGNORECASE)
INTERLOCK_RE = re.compile(r"\bInterlock=(?P<interlock>[^\s]+)", re.IGNORECASE)
DETAILS_RE = re.compile(r'Details="(?P<details>.*)"', re.IGNORECASE)


def parse_status(reply: str) -> CosconStatus:
    mode_match = MODE_RE.search(reply)
    interlock_match = INTERLOCK_RE.search(reply)
    details_match = DETAILS_RE.search(reply)

    if not mode_match:
        raise TestError(f"Could not parse COSCON Mode from: {reply!r}")
    if not interlock_match:
        raise TestError(
            "Could not verify COSCON interlock because GetStatus did not include "
            f"an Interlock field: {reply!r}"
        )

    return CosconStatus(
        mode=mode_match.group("mode"),
        interlock=interlock_match.group("interlock"),
        details=details_match.group("details") if details_match else "",
        raw=reply,
    )


def get_status(client: CosconUDP) -> CosconStatus:
    return parse_status(client.send("GetStatus"))


def require_interlock_ok(status: CosconStatus, context: str) -> None:
    if status.interlock.lower() != "ok":
        raise TestError(
            f"Interlock is not OK {context}: {status.interlock} "
            f"({status.details})"
        )


def read_pressure_checked(
    gauge: XGS600Pressure,
    logger: ReportLogger,
    *,
    abort_pressure: float,
    context: str,
) -> float:
    pressure = gauge.read_mbar()
    logger.add_pressure(pressure, context)
    if pressure >= abort_pressure:
        raise PressureSafetyError(
            f"Pressure reached {pressure:.6e} mbar, at or above the "
            f"{abort_pressure:.6e} mbar Degas cutoff."
        )
    return pressure


def require_safe_initial_conditions(
    client: CosconUDP,
    gauge: XGS600Pressure,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> None:
    status = get_status(client)
    if status.mode.lower() != "off":
        raise TestError(
            f"Test refused: initial Mode must be Off, but COSCON reports "
            f"{status.mode} ({status.details})."
        )
    require_interlock_ok(status, "before Degas")

    pressures = []
    for index in range(3):
        pressure = gauge.read_mbar()
        logger.add_pressure(pressure, f"preflight {index + 1}/3")
        pressures.append(pressure)
        if index < 2:
            time.sleep(0.5)

    highest = max(pressures)
    if highest > args.start_pressure_max:
        raise PressureSafetyError(
            f"Test refused: highest preflight pressure was {highest:.6e} mbar, "
            f"above the start limit {args.start_pressure_max:.6e} mbar."
        )


def wait_for_degas(
    client: CosconUDP,
    gauge: XGS600Pressure,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> CosconStatus:
    deadline = time.monotonic() + args.degas_start_timeout
    last_status: Optional[CosconStatus] = None

    while time.monotonic() < deadline:
        status = get_status(client)
        last_status = status
        require_interlock_ok(status, "while starting Degas")

        read_pressure_checked(
            gauge,
            logger,
            abort_pressure=args.abort_pressure,
            context="waiting for Mode=Degas",
        )

        mode = status.mode.lower()
        if mode == "degas":
            return status
        if mode == "error":
            raise TestError(f"COSCON entered Error mode: {status.details}")
        if mode not in {"off", "switchingtostandby", "standby"}:
            raise TestError(
                f"Unexpected COSCON mode while starting Degas: "
                f"{status.mode} ({status.details})"
            )

        time.sleep(args.poll)

    raise TestError(
        f"Timeout waiting for Mode=Degas. Last status: "
        f"{last_status.raw if last_status else 'no status received'}"
    )


def wait_for_modes(
    client: CosconUDP,
    target_modes: set[str],
    timeout_s: float,
    *,
    allow_interlock_not_ok: bool,
    logger: ReportLogger,
) -> CosconStatus:
    target_lower = {mode.lower() for mode in target_modes}
    deadline = time.monotonic() + timeout_s
    last_status: Optional[CosconStatus] = None

    while time.monotonic() < deadline:
        status = get_status(client)
        last_status = status

        if not allow_interlock_not_ok:
            require_interlock_ok(status, f"while waiting for {sorted(target_modes)}")

        if status.mode.lower() in target_lower:
            return status
        time.sleep(DEFAULT_POLL_S)

    raise TestError(
        f"Timeout waiting for Mode in {sorted(target_modes)}. Last status: "
        f"{last_status.raw if last_status else 'no status received'}"
    )


def request_safe_stop(
    client: CosconUDP,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> bool:
    """Best-effort documented Degas stop: Standby first, then Off."""

    logger.add("SAFE STOP: requesting Standby.")
    try:
        client.send("SwitchToStandby")
        status = wait_for_modes(
            client,
            {"Standby", "Off"},
            args.standby_timeout,
            allow_interlock_not_ok=True,
            logger=logger,
        )
        logger.add(
            f"SAFE STOP: reached Mode={status.mode}, "
            f"Interlock={status.interlock}, Details={status.details!r}"
        )
    except Exception as exc:
        logger.add(f"SAFE STOP warning: Standby could not be confirmed: {exc}")

    logger.add("SAFE STOP: requesting Off.")
    try:
        client.send("SwitchToOff")
        status = wait_for_modes(
            client,
            {"Off"},
            args.off_timeout,
            allow_interlock_not_ok=True,
            logger=logger,
        )
        logger.add(
            f"SAFE STOP: final Off confirmed, Interlock={status.interlock}, "
            f"Details={status.details!r}"
        )
        return True
    except Exception as exc:
        logger.add(
            "CRITICAL: automatic return to Off could not be confirmed: "
            f"{exc}. Use the local COSCON controls immediately."
        )
        return False


def show_preflight(args: argparse.Namespace) -> None:
    print(
        "\nPRE-FLIGHT — verify physically before continuing\n"
        "------------------------------------------------\n"
        "1. A trained operator is present at the chamber.\n"
        "2. The IQE 11/35 source cable is correctly oriented and connected.\n"
        "3. The source is under vacuum and the XGS600 is reading correctly.\n"
        "4. The argon leak valve is closed; no gas is being admitted.\n"
        "5. COSCON shows no fault/interlock alarm.\n"
        "6. SpecsLab, the COSCON web page and Phase 2 will not issue commands.\n"
        "7. The physical COSCON controls are immediately accessible.\n"
        "\n"
        "This brief test will start Degas, verify Mode=Degas, observe it for "
        f"{args.observe_seconds:.1f} seconds, then request Standby and Off.\n"
        f"It will abort at pressure >= {args.abort_pressure:.1e} mbar or if "
        "pressure monitoring is lost.\n"
    )


def run_normal_test(args: argparse.Namespace) -> int:
    logger = ReportLogger()
    report_dir = Path(args.report_dir)
    client = CosconUDP(args.ip, args.udp_port, args.udp_timeout, logger)

    gauge: Optional[XGS600Pressure] = None
    degas_requested = False
    final_off_confirmed = False
    result = "failure"
    reason = "Test did not complete."

    try:
        logger.add("Starting supervised brief Degas test.")
        client.send("Info")
        gauge = XGS600Pressure(
            args.xgs_port,
            args.xgs_baud,
            args.xgs_timeout,
            logger,
        )

        require_safe_initial_conditions(client, gauge, logger, args)
        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        show_preflight(args)
        typed = input(
            f'Type exactly "{CONFIRMATION_PHRASE}" to start, '
            "or press Enter to cancel:\n> "
        ).strip()
        if typed != CONFIRMATION_PHRASE:
            result = "cancelled"
            reason = "Cancelled before any Degas command was sent."
            logger.add(reason)
            return 2

        # Re-check state and pressure immediately before the write command.
        require_safe_initial_conditions(client, gauge, logger, args)

        logger.add("Requesting Degas.")
        reply = client.send("SwitchToDegas")
        if "OK" not in reply.upper():
            raise TestError(f"Unexpected SwitchToDegas reply: {reply}")
        degas_requested = True

        status = wait_for_degas(client, gauge, logger, args)
        logger.add(
            f"Degas confirmed: Mode={status.mode}, "
            f"Interlock={status.interlock}, Details={status.details!r}"
        )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        logger.add(
            f"Observing Degas for {args.observe_seconds:.1f} s with active "
            "pressure/interlock monitoring."
        )
        deadline = time.monotonic() + args.observe_seconds
        while time.monotonic() < deadline:
            status = get_status(client)
            require_interlock_ok(status, "during Degas observation")

            pressure = read_pressure_checked(
                gauge,
                logger,
                abort_pressure=args.abort_pressure,
                context="Degas observation",
            )

            mode = status.mode.lower()
            if mode == "error":
                raise TestError(f"COSCON entered Error mode: {status.details}")
            if mode in {"off", "standby"}:
                logger.add(
                    f"Degas ended before the observation timer expired: "
                    f"Mode={status.mode}, Details={status.details!r}"
                )
                break
            if mode != "degas":
                raise TestError(
                    f"Unexpected mode during Degas: {status.mode} "
                    f"({status.details})"
                )

            time.sleep(
                min(
                    args.poll,
                    max(0.0, deadline - time.monotonic()),
                )
            )

        final_off_confirmed = request_safe_stop(client, logger, args)
        if not final_off_confirmed:
            raise TestError("Degas was observed, but final Mode=Off was not confirmed.")

        final_status = get_status(client)
        if final_status.mode.lower() != "off":
            raise TestError(
                f"Final status is not Off: {final_status.raw}"
            )
        if final_status.interlock.lower() != "ok":
            raise TestError(
                f"Final Mode=Off was reached, but Interlock is "
                f"{final_status.interlock}: {final_status.details}"
            )

        client.send("GetTargetValues")
        client.send("GetMonitorValues")
        client.send("GetDiagnosticValues")

        result = "success"
        reason = (
            "Mode=Degas was confirmed, pressure/interlock monitoring remained "
            "available, and the COSCON returned to Mode=Off."
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
        if degas_requested and not final_off_confirmed:
            logger.add("Failsafe path activated.")
            final_off_confirmed = request_safe_stop(client, logger, args)

        if gauge is not None:
            gauge.close()

        report_path = logger.save(
            report_dir,
            result=result,
            reason=reason,
            args=args,
            final_off_confirmed=final_off_confirmed,
        )
        print(f"\nReport saved:\n  {report_path.resolve()}")


def run_safe_stop_only(args: argparse.Namespace) -> int:
    logger = ReportLogger()
    client = CosconUDP(args.ip, args.udp_port, args.udp_timeout, logger)
    result = "failure"
    reason = "Safe stop did not complete."
    final_off_confirmed = False

    try:
        logger.add("Starting COSCON safe-stop-only helper.")
        client.send("Info")
        status = get_status(client)
        logger.add(
            f"Initial status: Mode={status.mode}, Interlock={status.interlock}, "
            f"Details={status.details!r}"
        )

        final_off_confirmed = request_safe_stop(client, logger, args)
        if not final_off_confirmed:
            raise TestError("Could not confirm final Mode=Off.")

        result = "success"
        reason = "Final Mode=Off confirmed."
        logger.add("SUCCESS: " + reason)
        return 0

    except Exception as exc:
        reason = str(exc)
        logger.add("ERROR: " + reason)
        return 1

    finally:
        report_path = logger.save(
            Path(args.report_dir),
            result=result,
            reason=reason,
            args=args,
            final_off_confirmed=final_off_confirmed,
        )
        print(f"\nReport saved:\n  {report_path.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supervised brief COSCON IS Degas transition test."
    )
    parser.add_argument("--ip", default=DEFAULT_IP)
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument("--udp-timeout", type=float, default=DEFAULT_UDP_TIMEOUT_S)

    parser.add_argument("--xgs-port", default=DEFAULT_XGS_PORT)
    parser.add_argument("--xgs-baud", type=int, default=DEFAULT_XGS_BAUD)
    parser.add_argument("--xgs-timeout", type=float, default=DEFAULT_XGS_TIMEOUT_S)

    parser.add_argument(
        "--start-pressure-max",
        type=float,
        default=DEFAULT_START_PRESSURE_MAX_MBAR,
    )
    parser.add_argument(
        "--abort-pressure",
        type=float,
        default=DEFAULT_ABORT_PRESSURE_MBAR,
    )
    parser.add_argument(
        "--degas-start-timeout",
        type=float,
        default=DEFAULT_DEGAS_START_TIMEOUT_S,
    )
    parser.add_argument(
        "--standby-timeout",
        type=float,
        default=DEFAULT_STANDBY_TIMEOUT_S,
    )
    parser.add_argument(
        "--off-timeout",
        type=float,
        default=DEFAULT_OFF_TIMEOUT_S,
    )
    parser.add_argument(
        "--observe-seconds",
        type=float,
        default=DEFAULT_OBSERVE_S,
    )
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S)
    parser.add_argument(
        "--report-dir",
        default="COSCON Diagnostic Reports",
    )
    parser.add_argument(
        "--safe-stop-only",
        action="store_true",
        help="Request Standby and then Off without starting Degas.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.start_pressure_max <= 0:
        raise SystemExit("--start-pressure-max must be positive.")
    if args.abort_pressure <= args.start_pressure_max:
        raise SystemExit(
            "--abort-pressure must be greater than --start-pressure-max."
        )
    if args.observe_seconds < 0:
        raise SystemExit("--observe-seconds cannot be negative.")
    if args.poll <= 0:
        raise SystemExit("--poll must be positive.")

    if args.safe_stop_only:
        return run_safe_stop_only(args)
    return run_normal_test(args)


if __name__ == "__main__":
    sys.exit(main())
