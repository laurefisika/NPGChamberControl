#!/usr/bin/env python3
"""
COSCON IS UDP Operating verification v3
========================================

Purpose
-------
Repeat the successful manual 10 mA / 2250 V sputtering start through the
official UDP interface, while reproducing the successful preparation as closely
as practical:

    complete Degas performed manually
    -> natural Mode=Standby
    -> argon stabilized near 2e-5 mbar
    -> 60 s verified Standby/pressure conditioning
    -> ValidateOperateTarget
    -> SwitchToOperate once
    -> verify measured energy and emission, not only Mode=Operating
    -> 60 s stable Operating verification
    -> SwitchToStandby
    -> leave the source in Standby

The normal path deliberately does NOT send SwitchToOff, because the successful
manual procedure and the normal Phase 2 workflow continue from Standby.

On a fault, pressure emergency, interlock change, communication failure after
activation, or Ctrl+C, the script attempts Standby first and Off only if a safe
Standby/Off state cannot be confirmed.

Strict command whitelist
------------------------
Read:
    Info
    GetStatus
    GetTargetValues
    GetMonitorValues
    GetDiagnosticValues

Write:
    ValidateOperateTarget Emission=<number> Energy=<number>
    SwitchToOperate Emission=<number> Energy=<number>
    SwitchToStandby
    SwitchToOff

No Reset, Degas, preset write/delete, or network command exists in this file.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import socket
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError as exc:
    raise SystemExit(
        "pyserial is required. Run from the project .venv or install it with:\n"
        "    python -m pip install pyserial"
    ) from exc


DEFAULT_IP = "192.168.236.186"
DEFAULT_UDP_PORT = 2005
DEFAULT_UDP_TIMEOUT_S = 2.0

DEFAULT_XGS_PORT = "COM6"
DEFAULT_XGS_BAUD = 9600
DEFAULT_XGS_TIMEOUT_S = 1.2

DEFAULT_EMISSION_A = 0.010
DEFAULT_ENERGY_V = 2250.0

DEFAULT_PRESSURE_MIN_MBAR = 1.0e-5
DEFAULT_PRESSURE_MAX_MBAR = 5.0e-5
DEFAULT_PRESSURE_EMERGENCY_MAX_MBAR = 1.0e-4

DEFAULT_STANDBY_CONDITIONING_S = 60.0
DEFAULT_OPERATE_TRANSITION_TIMEOUT_S = 35.0
DEFAULT_STABILITY_TIMEOUT_S = 20.0
DEFAULT_OPERATING_HOLD_S = 60.0
DEFAULT_STANDBY_TIMEOUT_S = 25.0
DEFAULT_OFF_TIMEOUT_S = 25.0
DEFAULT_POLL_S = 0.55

DEFAULT_ENERGY_TOLERANCE_V = 50.0
DEFAULT_EMISSION_TOLERANCE_A = 0.001
DEFAULT_STABLE_SAMPLES = 5
DEFAULT_MAX_CONSECUTIVE_SOFT_BAD = 3

CONFIRMATION_PHRASE = "START UDP SPUTTER TEST 10mA 2250V"
VALVE_CLOSED_PHRASE = "ARGON VALVE CLOSED"

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


class TestError(RuntimeError):
    pass


class CommandRejected(TestError):
    pass


class DeviceFault(TestError):
    pass


class PressureFault(TestError):
    pass


class CommunicationFault(TestError):
    pass


@dataclass
class Status:
    mode: str
    interlock: str
    details: str
    raw: str


@dataclass
class Monitor:
    energy_v: float
    filament_a: float
    emission_a: float
    raw: str


@dataclass
class TelemetryRow:
    timestamp_iso: str
    elapsed_s: float
    phase: str
    mode: str
    interlock: str
    details: str
    target_energy_v: float
    target_emission_a: float
    measured_energy_v: Optional[float]
    measured_emission_a: Optional[float]
    filament_current_a: Optional[float]
    pressure_mbar: Optional[float]
    energy_error_v: Optional[float]
    emission_error_a: Optional[float]
    note: str


def command_allowed(command: str) -> bool:
    return (
        command in EXACT_COMMANDS
        or bool(VALIDATE_RE.fullmatch(command))
        or bool(OPERATE_RE.fullmatch(command))
    )


class ReportLogger:
    def __init__(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = output_dir
        self.raw_path = output_dir / f"coscon_udp_operate_v3_{stamp}_raw.log"
        self.csv_path = output_dir / f"coscon_udp_operate_v3_{stamp}_telemetry.csv"
        self.summary_path = output_dir / f"coscon_udp_operate_v3_{stamp}_summary.txt"

        self.start_monotonic = time.monotonic()
        self.raw_handle = self.raw_path.open("w", encoding="utf-8", buffering=1)
        self.csv_handle = self.csv_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.DictWriter(
            self.csv_handle,
            fieldnames=list(TelemetryRow.__dataclass_fields__.keys()),
        )
        self.csv_writer.writeheader()
        self.csv_handle.flush()

        self.pressures: list[float] = []
        self.energies: list[float] = []
        self.emissions: list[float] = []
        self.operating_started_elapsed: Optional[float] = None
        self.stable_started_elapsed: Optional[float] = None
        self.result = "failure"
        self.reason = "Test did not complete."
        self.final_mode = "unconfirmed"
        self.final_interlock = "unconfirmed"

    def elapsed(self) -> float:
        return time.monotonic() - self.start_monotonic

    def add(self, message: str) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        line = f"[{timestamp}] [{self.elapsed():10.3f} s] {message}"
        self.raw_handle.write(line + "\n")
        print(line)

    def telemetry(
        self,
        *,
        phase: str,
        status: Status,
        target_energy_v: float,
        target_emission_a: float,
        monitor: Optional[Monitor],
        pressure_mbar: Optional[float],
        note: str = "",
    ) -> None:
        energy = monitor.energy_v if monitor else None
        emission = monitor.emission_a if monitor else None
        filament = monitor.filament_a if monitor else None

        if pressure_mbar is not None:
            self.pressures.append(pressure_mbar)
        if energy is not None:
            self.energies.append(energy)
        if emission is not None:
            self.emissions.append(emission)

        row = TelemetryRow(
            timestamp_iso=datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            ),
            elapsed_s=self.elapsed(),
            phase=phase,
            mode=status.mode,
            interlock=status.interlock,
            details=status.details,
            target_energy_v=target_energy_v,
            target_emission_a=target_emission_a,
            measured_energy_v=energy,
            measured_emission_a=emission,
            filament_current_a=filament,
            pressure_mbar=pressure_mbar,
            energy_error_v=(
                energy - target_energy_v if energy is not None else None
            ),
            emission_error_a=(
                emission - target_emission_a if emission is not None else None
            ),
            note=note,
        )
        self.csv_writer.writerow(asdict(row))
        self.csv_handle.flush()

    def close_and_write_summary(
        self,
        *,
        args: argparse.Namespace,
        activation_requested: bool,
        standby_confirmed: bool,
        off_confirmed: bool,
    ) -> None:
        lines = [
            "COSCON IS UDP OPERATING VERIFICATION V3",
            "========================================",
            f"Timestamp: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"Result: {self.result}",
            f"Reason: {self.reason}",
            f"COSCON: {args.ip}:{args.udp_port}",
            f"Pressure gauge: {args.xgs_port} at {args.xgs_baud} baud",
            f"Target emission: {args.emission:.6e} A",
            f"Target energy: {args.energy:.1f} V",
            f"Standby conditioning: {args.conditioning:.1f} s",
            f"Requested Operating hold: {args.hold:.1f} s",
            f"Activation command requested: {activation_requested}",
            f"Standby confirmed at end/failsafe: {standby_confirmed}",
            f"Off confirmed at end/failsafe: {off_confirmed}",
            f"Final observed mode: {self.final_mode}",
            f"Final observed interlock: {self.final_interlock}",
        ]

        if self.operating_started_elapsed is not None:
            lines.append(
                f"Mode=Operating first confirmed at: "
                f"{self.operating_started_elapsed:.3f} s"
            )
        if self.stable_started_elapsed is not None:
            lines.append(
                f"Stable measured output first confirmed at: "
                f"{self.stable_started_elapsed:.3f} s"
            )

        if self.pressures:
            lines.append(
                f"Pressure range: {min(self.pressures):.6e} to "
                f"{max(self.pressures):.6e} mbar"
            )
        if self.energies:
            lines.append(
                f"Measured energy range: {min(self.energies):.6g} to "
                f"{max(self.energies):.6g} V"
            )
        if self.emissions:
            lines.append(
                f"Measured emission range: {min(self.emissions):.6e} to "
                f"{max(self.emissions):.6e} A"
            )

        lines.extend([
            "",
            "Normal planned sequence:",
            "  Standby",
            "  -> 60 s stable pressure/Standby conditioning",
            "  -> ValidateOperateTarget",
            "  -> SwitchToOperate once",
            "  -> verify Operating and measured 2250 V / 10 mA",
            "  -> stable hold",
            "  -> SwitchToStandby",
            "  -> leave COSCON in Standby",
            "",
            "Files:",
            f"  Raw log: {self.raw_path.name}",
            f"  Telemetry CSV: {self.csv_path.name}",
        ])

        self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.raw_handle.close()
        self.csv_handle.close()

        print(
            "\nReports saved:\n"
            f"  Summary:   {self.summary_path.resolve()}\n"
            f"  Telemetry: {self.csv_path.resolve()}\n"
            f"  Raw log:   {self.raw_path.resolve()}\n"
        )


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

    def send(self, command: str) -> str:
        if not command_allowed(command):
            raise TestError(f"Blocked COSCON command: {command!r}")

        command_name = command.split()[0]
        payload = (command + "\r").encode("ascii")
        self.logger.add(f"COSCON -> {command}")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendto(payload, (self.ip, self.port))
            try:
                data, sender = sock.recvfrom(8192)
            except socket.timeout as exc:
                raise CommunicationFault(
                    f"No COSCON reply to {command!r} within "
                    f"{self.timeout_s:.1f} s."
                ) from exc

        if sender[0] != self.ip:
            raise CommunicationFault(
                f"Unexpected COSCON reply source: {sender[0]}:{sender[1]}"
            )

        reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
        self.logger.add(f"COSCON <- {reply}")

        if not reply:
            raise CommunicationFault(f"Empty reply to {command!r}.")

        if re.match(
            rf"^{re.escape(command_name)}\s+ERROR\b",
            reply,
            re.IGNORECASE,
        ) or re.match(r"^ERROR\b", reply, re.IGNORECASE):
            raise CommandRejected(f"COSCON rejected {command!r}: {reply}")

        return reply


MODE_RE = re.compile(r"\bMode=([^\s]+)", re.IGNORECASE)
INTERLOCK_RE = re.compile(r"\bInterlock=([^\s]+)", re.IGNORECASE)
DETAILS_RE = re.compile(r'Details="([^"]*)"', re.IGNORECASE)
NUMBER_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]*)="
    r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)


def parse_status(reply: str) -> Status:
    mode_match = MODE_RE.search(reply)
    interlock_match = INTERLOCK_RE.search(reply)
    details_match = DETAILS_RE.search(reply)

    if not mode_match:
        raise TestError(f"Could not parse Mode from: {reply!r}")
    if not interlock_match:
        raise TestError(f"Could not parse Interlock from: {reply!r}")

    return Status(
        mode=mode_match.group(1),
        interlock=interlock_match.group(1),
        details=details_match.group(1) if details_match else "",
        raw=reply,
    )


def parse_monitor(reply: str) -> Monitor:
    fields = {key: float(value) for key, value in NUMBER_RE.findall(reply)}
    missing = [
        key for key in ("VEnergy", "IFilament", "IEmission")
        if key not in fields
    ]
    if missing:
        raise TestError(
            f"Missing monitor fields {missing} in reply: {reply!r}"
        )
    return Monitor(
        energy_v=fields["VEnergy"],
        filament_a=fields["IFilament"],
        emission_a=fields["IEmission"],
        raw=reply,
    )


def get_status(client: CosconUDP) -> Status:
    return parse_status(client.send("GetStatus"))


def get_monitor(client: CosconUDP) -> Monitor:
    return parse_monitor(client.send("GetMonitorValues"))


def require_interlock_ok(status: Status, context: str) -> None:
    if status.interlock.lower() != "ok":
        raise DeviceFault(
            f"Interlock is not OK {context}: "
            f"{status.interlock} ({status.details})"
        )


def require_no_error_mode(status: Status, context: str) -> None:
    if status.mode.lower() == "error":
        raise DeviceFault(
            f"COSCON entered Mode=Error {context}: {status.details}"
        )


class XGS600:
    COMMAND = b"#0002USYNTH\r"
    VALUE_RE = re.compile(
        r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
    )

    def __init__(
        self,
        port: str,
        baud: int,
        timeout_s: float,
        logger: ReportLogger,
    ) -> None:
        self.logger = logger
        self.timeout_s = timeout_s
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
                f"Could not open XGS600 on {port}: {exc}. "
                "Close Phase 2 and every other program using this COM port."
            ) from exc

    def close(self) -> None:
        try:
            if self.ser.is_open:
                try:
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
                except Exception:
                    pass
                self.ser.close()
        except Exception as exc:
            self.logger.add(f"XGS600 cleanup warning: {exc}")

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
                elif (
                    buffer
                    and last_data_at is not None
                    and time.monotonic() - last_data_at >= 0.08
                ):
                    break
                time.sleep(0.02)

            if not buffer:
                buffer.extend(self.ser.read(100))

            text = bytes(buffer).decode("ascii", errors="ignore").strip()

        except Exception as exc:
            raise PressureFault(f"XGS600 communication failed: {exc}") from exc

        cleaned = text.lstrip(">").strip()
        if cleaned.lower() in {"", "nan", "+nan", "-nan"}:
            raise PressureFault(
                f"Pressure unavailable: XGS600 returned {text!r}."
            )

        match = self.VALUE_RE.search(cleaned)
        if not match:
            raise PressureFault(
                f"Could not parse pressure from XGS600 reply {text!r}."
            )

        value = float(match.group(0))
        if not math.isfinite(value) or value <= 0:
            raise PressureFault(f"Invalid pressure value: {value!r}.")
        return value


def check_pressure(
    pressure: float,
    args: argparse.Namespace,
    *,
    context: str,
) -> bool:
    if pressure >= args.pressure_emergency_max:
        raise PressureFault(
            f"Emergency pressure {pressure:.6e} mbar during {context}; "
            f"limit is {args.pressure_emergency_max:.6e} mbar."
        )

    return args.pressure_min <= pressure <= args.pressure_max


def read_full_snapshot(
    client: CosconUDP,
    gauge: XGS600,
    logger: ReportLogger,
    args: argparse.Namespace,
    *,
    phase: str,
    note: str = "",
) -> tuple[Status, Monitor, float]:
    status = get_status(client)
    require_interlock_ok(status, phase)
    require_no_error_mode(status, phase)
    monitor = get_monitor(client)
    pressure = gauge.read_mbar()
    logger.add(f"XGS600 pressure: {pressure:.6e} mbar")
    check_pressure(pressure, args, context=phase)

    logger.telemetry(
        phase=phase,
        status=status,
        target_energy_v=args.energy,
        target_emission_a=args.emission,
        monitor=monitor,
        pressure_mbar=pressure,
        note=note,
    )
    return status, monitor, pressure


def capture_diagnostics(
    client: CosconUDP,
    logger: ReportLogger,
    label: str,
) -> None:
    logger.add(f"DIAGNOSTIC SNAPSHOT START [{label}]")
    for command in (
        "GetStatus",
        "GetTargetValues",
        "GetMonitorValues",
        "GetDiagnosticValues",
    ):
        try:
            client.send(command)
        except Exception as exc:
            logger.add(f"{command} failed during diagnostic snapshot: {exc}")
    logger.add(f"DIAGNOSTIC SNAPSHOT END [{label}]")


def wait_for_mode(
    client: CosconUDP,
    target_modes: set[str],
    timeout_s: float,
    poll_s: float,
    *,
    allow_bad_interlock: bool,
) -> Status:
    targets = {mode.lower() for mode in target_modes}
    deadline = time.monotonic() + timeout_s
    last: Optional[Status] = None

    while time.monotonic() < deadline:
        status = get_status(client)
        last = status

        if not allow_bad_interlock:
            require_interlock_ok(status, f"while waiting for {sorted(target_modes)}")

        if status.mode.lower() in targets:
            return status

        time.sleep(poll_s)

    raise TestError(
        f"Timeout waiting for Mode in {sorted(target_modes)}. "
        f"Last status: {last.raw if last else 'no status received'}"
    )


def safe_stop(
    client: CosconUDP,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> tuple[bool, bool]:
    standby_confirmed = False
    off_confirmed = False

    try:
        status = get_status(client)
        logger.final_mode = status.mode
        logger.final_interlock = status.interlock
        logger.add(
            f"SAFE STOP initial state: Mode={status.mode}, "
            f"Interlock={status.interlock}, Details={status.details!r}"
        )
    except Exception as exc:
        status = None
        logger.add(f"SAFE STOP initial status query failed: {exc}")

    if status is not None:
        mode = status.mode.lower()
        if mode == "standby":
            return True, False
        if mode == "off":
            return False, True
        if mode == "degassing":
            logger.add(
                "SAFE STOP detected Degassing; requesting Off directly."
            )
        else:
            logger.add("SAFE STOP requesting Standby once.")
            try:
                client.send("SwitchToStandby")
            except Exception as exc:
                logger.add(f"SwitchToStandby reply problem: {exc}")

            try:
                reached = wait_for_mode(
                    client,
                    {"Standby", "Off"},
                    args.standby_timeout,
                    args.poll,
                    allow_bad_interlock=True,
                )
                logger.final_mode = reached.mode
                logger.final_interlock = reached.interlock
                if reached.mode.lower() == "standby":
                    return True, False
                if reached.mode.lower() == "off":
                    return False, True
            except Exception as exc:
                logger.add(f"Standby/Off could not be confirmed: {exc}")

    logger.add("SAFE STOP requesting Off once.")
    try:
        client.send("SwitchToOff")
    except Exception as exc:
        logger.add(f"SwitchToOff reply problem: {exc}")

    try:
        final = wait_for_mode(
            client,
            {"Off"},
            args.off_timeout,
            args.poll,
            allow_bad_interlock=True,
        )
        logger.final_mode = final.mode
        logger.final_interlock = final.interlock
        off_confirmed = True
    except Exception as exc:
        logger.add(
            "CRITICAL: final Mode=Off could not be confirmed. "
            f"Use the local COSCON controls immediately. Details: {exc}"
        )

    return standby_confirmed, off_confirmed


def run_standby_conditioning(
    client: CosconUDP,
    gauge: XGS600,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> None:
    logger.add(
        f"Starting {args.conditioning:.1f} s verified Standby/pressure conditioning."
    )
    deadline = time.monotonic() + args.conditioning
    consecutive_outside = 0

    while time.monotonic() < deadline:
        status = get_status(client)
        require_interlock_ok(status, "during Standby conditioning")
        require_no_error_mode(status, "during Standby conditioning")

        if status.mode.lower() != "standby":
            raise TestError(
                f"Mode changed during conditioning: "
                f"{status.mode} ({status.details})"
            )

        monitor = get_monitor(client)
        pressure = gauge.read_mbar()
        logger.add(f"XGS600 pressure: {pressure:.6e} mbar")

        inside = check_pressure(
            pressure,
            args,
            context="Standby conditioning",
        )
        consecutive_outside = 0 if inside else consecutive_outside + 1

        logger.telemetry(
            phase="STANDBY_CONDITIONING",
            status=status,
            target_energy_v=args.energy,
            target_emission_a=args.emission,
            monitor=monitor,
            pressure_mbar=pressure,
            note=(
                "pressure inside normal window"
                if inside
                else f"pressure outside normal window "
                     f"({consecutive_outside}/"
                     f"{args.max_consecutive_soft_bad})"
            ),
        )

        if consecutive_outside >= args.max_consecutive_soft_bad:
            raise PressureFault(
                f"Pressure remained outside "
                f"[{args.pressure_min:.6e}, {args.pressure_max:.6e}] mbar "
                f"for {consecutive_outside} consecutive samples."
            )

        remaining = max(0.0, deadline - time.monotonic())
        print(
            f"\rStandby conditioning remaining: {remaining:5.1f} s   ",
            end="",
            flush=True,
        )
        time.sleep(args.poll)

    print()
    logger.add("Standby/pressure conditioning completed.")


def wait_for_operating(
    client: CosconUDP,
    gauge: XGS600,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> Status:
    logger.add(
        f"Waiting up to {args.operate_timeout:.1f} s for Mode=Operating."
    )
    deadline = time.monotonic() + args.operate_timeout
    consecutive_pressure_outside = 0

    while time.monotonic() < deadline:
        status = get_status(client)
        require_interlock_ok(status, "during SwitchToOperate transition")

        if status.mode.lower() == "error":
            capture_diagnostics(client, logger, "ERROR DURING TRANSITION")
            raise DeviceFault(
                f"COSCON entered Mode=Error during transition: "
                f"{status.details}"
            )

        if status.mode.lower() not in {
            "standby",
            "switchingtooperate",
            "operating",
        }:
            raise TestError(
                f"Unexpected mode during transition: "
                f"{status.mode} ({status.details})"
            )

        monitor = get_monitor(client)
        pressure = gauge.read_mbar()
        logger.add(f"XGS600 pressure: {pressure:.6e} mbar")

        inside = check_pressure(
            pressure,
            args,
            context="SwitchToOperate transition",
        )
        consecutive_pressure_outside = (
            0 if inside else consecutive_pressure_outside + 1
        )

        logger.telemetry(
            phase="SWITCHING_TO_OPERATE",
            status=status,
            target_energy_v=args.energy,
            target_emission_a=args.emission,
            monitor=monitor,
            pressure_mbar=pressure,
            note=(
                "normal transition monitoring"
                if inside
                else f"pressure outside normal window "
                     f"({consecutive_pressure_outside}/"
                     f"{args.max_consecutive_soft_bad})"
            ),
        )

        if consecutive_pressure_outside >= args.max_consecutive_soft_bad:
            raise PressureFault(
                "Pressure remained outside the normal window during activation."
            )

        if status.mode.lower() == "operating":
            logger.operating_started_elapsed = logger.elapsed()
            logger.add("Mode=Operating confirmed.")
            return status

        time.sleep(args.poll)

    capture_diagnostics(client, logger, "OPERATING TIMEOUT")
    raise TestError("Timeout waiting for Mode=Operating.")


def wait_for_stable_output(
    client: CosconUDP,
    gauge: XGS600,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> None:
    logger.add(
        "Mode=Operating is not enough; verifying measured energy and emission."
    )

    deadline = time.monotonic() + args.stability_timeout
    consecutive_good = 0
    consecutive_pressure_outside = 0

    while time.monotonic() < deadline:
        status, monitor, pressure = read_full_snapshot(
            client,
            gauge,
            logger,
            args,
            phase="VERIFY_STABLE_OUTPUT",
        )

        if status.mode.lower() != "operating":
            raise TestError(
                f"Expected Operating while verifying output, got "
                f"{status.mode} ({status.details})"
            )

        pressure_inside = (
            args.pressure_min <= pressure <= args.pressure_max
        )
        consecutive_pressure_outside = (
            0 if pressure_inside else consecutive_pressure_outside + 1
        )
        if consecutive_pressure_outside >= args.max_consecutive_soft_bad:
            raise PressureFault(
                "Pressure remained outside the normal window while "
                "verifying stable output."
            )

        energy_good = (
            abs(monitor.energy_v - args.energy)
            <= args.energy_tolerance
        )
        emission_good = (
            abs(monitor.emission_a - args.emission)
            <= args.emission_tolerance
        )

        if energy_good and emission_good and pressure_inside:
            consecutive_good += 1
        else:
            consecutive_good = 0

        logger.add(
            "Output verification: "
            f"VEnergy={monitor.energy_v:.2f} V "
            f"(target {args.energy:.1f}, good={energy_good}); "
            f"IEmission={monitor.emission_a * 1000:.3f} mA "
            f"(target {args.emission * 1000:.3f}, good={emission_good}); "
            f"stable samples={consecutive_good}/{args.stable_samples}"
        )

        if consecutive_good >= args.stable_samples:
            logger.stable_started_elapsed = logger.elapsed()
            logger.add(
                "Stable measured output confirmed: energy, emission, "
                "pressure and interlock are all within limits."
            )
            return

        time.sleep(args.poll)

    capture_diagnostics(client, logger, "OUTPUT STABILITY TIMEOUT")
    raise TestError(
        "Mode=Operating was reached, but stable measured output was not "
        "confirmed within the allowed time."
    )


def run_operating_hold(
    client: CosconUDP,
    gauge: XGS600,
    logger: ReportLogger,
    args: argparse.Namespace,
) -> None:
    logger.add(
        f"Starting {args.hold:.1f} s stable Operating verification hold."
    )
    deadline = time.monotonic() + args.hold

    consecutive_pressure_outside = 0
    consecutive_energy_bad = 0
    consecutive_emission_bad = 0

    while time.monotonic() < deadline:
        status = get_status(client)
        require_interlock_ok(status, "during Operating hold")

        if status.mode.lower() == "error":
            capture_diagnostics(client, logger, "ERROR DURING HOLD")
            raise DeviceFault(
                f"COSCON entered Mode=Error during hold: {status.details}"
            )

        if status.mode.lower() != "operating":
            raise TestError(
                f"Unexpected mode during Operating hold: "
                f"{status.mode} ({status.details})"
            )

        monitor = get_monitor(client)
        pressure = gauge.read_mbar()
        logger.add(f"XGS600 pressure: {pressure:.6e} mbar")

        if pressure >= args.pressure_emergency_max:
            capture_diagnostics(client, logger, "PRESSURE EMERGENCY")
            raise PressureFault(
                f"Emergency pressure {pressure:.6e} mbar during Operating."
            )

        pressure_inside = (
            args.pressure_min <= pressure <= args.pressure_max
        )
        energy_good = (
            abs(monitor.energy_v - args.energy)
            <= args.energy_tolerance
        )
        emission_good = (
            abs(monitor.emission_a - args.emission)
            <= args.emission_tolerance
        )

        consecutive_pressure_outside = (
            0 if pressure_inside else consecutive_pressure_outside + 1
        )
        consecutive_energy_bad = (
            0 if energy_good else consecutive_energy_bad + 1
        )
        consecutive_emission_bad = (
            0 if emission_good else consecutive_emission_bad + 1
        )

        if monitor.energy_v < 0.80 * args.energy:
            capture_diagnostics(client, logger, "MAJOR ENERGY COLLAPSE")
            raise DeviceFault(
                f"Measured energy collapsed to {monitor.energy_v:.2f} V "
                f"after stable operation."
            )

        logger.telemetry(
            phase="OPERATING_HOLD",
            status=status,
            target_energy_v=args.energy,
            target_emission_a=args.emission,
            monitor=monitor,
            pressure_mbar=pressure,
            note=(
                f"pressure_bad={consecutive_pressure_outside}; "
                f"energy_bad={consecutive_energy_bad}; "
                f"emission_bad={consecutive_emission_bad}"
            ),
        )

        if (
            consecutive_pressure_outside
            >= args.max_consecutive_soft_bad
        ):
            raise PressureFault(
                "Pressure remained outside the normal sputtering window."
            )

        if (
            consecutive_energy_bad
            >= args.max_consecutive_soft_bad
        ):
            capture_diagnostics(client, logger, "ENERGY OUT OF TOLERANCE")
            raise DeviceFault(
                f"Measured energy remained outside "
                f"{args.energy:.1f} ± {args.energy_tolerance:.1f} V."
            )

        if (
            consecutive_emission_bad
            >= args.max_consecutive_soft_bad
        ):
            capture_diagnostics(client, logger, "EMISSION OUT OF TOLERANCE")
            raise DeviceFault(
                f"Measured emission remained outside "
                f"{args.emission * 1000:.3f} ± "
                f"{args.emission_tolerance * 1000:.3f} mA."
            )

        remaining = max(0.0, deadline - time.monotonic())
        print(
            f"\rOperating verification remaining: {remaining:5.1f} s | "
            f"E={monitor.energy_v:7.2f} V | "
            f"Iem={monitor.emission_a * 1000:6.3f} mA | "
            f"P={pressure:.3e} mbar   ",
            end="",
            flush=True,
        )

        time.sleep(args.poll)

    print()
    logger.add("Stable Operating verification hold completed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="COSCON UDP Operating verification v3."
    )

    parser.add_argument("--ip", default=DEFAULT_IP)
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument(
        "--udp-timeout",
        type=float,
        default=DEFAULT_UDP_TIMEOUT_S,
    )

    parser.add_argument("--xgs-port", default=DEFAULT_XGS_PORT)
    parser.add_argument("--xgs-baud", type=int, default=DEFAULT_XGS_BAUD)
    parser.add_argument(
        "--xgs-timeout",
        type=float,
        default=DEFAULT_XGS_TIMEOUT_S,
    )

    parser.add_argument("--emission", type=float, default=DEFAULT_EMISSION_A)
    parser.add_argument("--energy", type=float, default=DEFAULT_ENERGY_V)

    parser.add_argument(
        "--pressure-min",
        type=float,
        default=DEFAULT_PRESSURE_MIN_MBAR,
    )
    parser.add_argument(
        "--pressure-max",
        type=float,
        default=DEFAULT_PRESSURE_MAX_MBAR,
    )
    parser.add_argument(
        "--pressure-emergency-max",
        type=float,
        default=DEFAULT_PRESSURE_EMERGENCY_MAX_MBAR,
    )

    parser.add_argument(
        "--conditioning",
        type=float,
        default=DEFAULT_STANDBY_CONDITIONING_S,
    )
    parser.add_argument(
        "--operate-timeout",
        type=float,
        default=DEFAULT_OPERATE_TRANSITION_TIMEOUT_S,
    )
    parser.add_argument(
        "--stability-timeout",
        type=float,
        default=DEFAULT_STABILITY_TIMEOUT_S,
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=DEFAULT_OPERATING_HOLD_S,
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
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_S)

    parser.add_argument(
        "--energy-tolerance",
        type=float,
        default=DEFAULT_ENERGY_TOLERANCE_V,
    )
    parser.add_argument(
        "--emission-tolerance",
        type=float,
        default=DEFAULT_EMISSION_TOLERANCE_A,
    )
    parser.add_argument(
        "--stable-samples",
        type=int,
        default=DEFAULT_STABLE_SAMPLES,
    )
    parser.add_argument(
        "--max-consecutive-soft-bad",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_SOFT_BAD,
    )

    parser.add_argument(
        "--output-dir",
        default="COSCON UDP Test Reports",
    )
    parser.add_argument(
        "--safe-stop-only",
        action="store_true",
        help="Do not activate; only attempt to reach Standby or Off.",
    )

    return parser


def validate_arguments(args: argparse.Namespace) -> None:
    if args.emission <= 0 or args.energy <= 0:
        raise SystemExit("Emission and energy must be positive.")
    if not (
        0 < args.pressure_min
        < args.pressure_max
        < args.pressure_emergency_max
    ):
        raise SystemExit("Invalid pressure thresholds.")
    if args.conditioning < 0 or args.hold < 0:
        raise SystemExit("Conditioning and hold times cannot be negative.")
    if args.stable_samples < 1:
        raise SystemExit("--stable-samples must be at least 1.")
    if args.max_consecutive_soft_bad < 1:
        raise SystemExit(
            "--max-consecutive-soft-bad must be at least 1."
        )


def show_preflight(args: argparse.Namespace) -> None:
    print(
        "\nCOSCON UDP ACTIVE SPUTTERING VERIFICATION V3\n"
        "--------------------------------------------\n"
        "This test activates the ion source and high voltage.\n"
        "\n"
        "Before continuing, verify physically:\n"
        "1. A trained operator is present at the chamber.\n"
        "2. A complete Degas cycle has finished naturally.\n"
        "3. COSCON is currently in Standby, not Off.\n"
        "4. Cable, connector and feedthrough inspection is complete.\n"
        "5. Argon pressure is stable near 2e-5 mbar.\n"
        "6. Sample and shutter positions are safe.\n"
        "7. The local COSCON controls are immediately accessible.\n"
        "8. The web UI, SpecsLab and Phase 2 are not sending commands.\n"
        "9. No other program is using XGS600 COM6.\n"
        "\n"
        f"Target: {args.emission * 1000:.3f} mA / "
        f"{args.energy:.1f} V\n"
        f"Standby conditioning: {args.conditioning:.1f} s\n"
        f"Stable Operating verification: {args.hold:.1f} s\n"
        f"Pressure window: {args.pressure_min:.1e} to "
        f"{args.pressure_max:.1e} mbar\n"
        f"Energy acceptance: ±{args.energy_tolerance:.1f} V\n"
        f"Emission acceptance: ±"
        f"{args.emission_tolerance * 1000:.3f} mA\n"
    )


def main() -> int:
    args = build_parser().parse_args()
    validate_arguments(args)

    logger = ReportLogger(Path(args.output_dir))
    client = CosconUDP(
        args.ip,
        args.udp_port,
        args.udp_timeout,
        logger,
    )

    gauge: Optional[XGS600] = None
    activation_requested = False
    standby_confirmed = False
    off_confirmed = False

    try:
        logger.add("Starting COSCON UDP Operating verification v3.")
        client.send("Info")

        if args.safe_stop_only:
            standby_confirmed, off_confirmed = safe_stop(
                client,
                logger,
                args,
            )
            if not (standby_confirmed or off_confirmed):
                raise TestError(
                    "Safe-stop-only mode could not confirm Standby or Off."
                )
            logger.result = "success"
            logger.reason = (
                f"Safe-stop-only completed with final mode "
                f"{'Standby' if standby_confirmed else 'Off'}."
            )
            return 0

        initial = get_status(client)
        logger.final_mode = initial.mode
        logger.final_interlock = initial.interlock
        require_interlock_ok(initial, "before test")
        require_no_error_mode(initial, "before test")

        if initial.mode.lower() != "standby":
            raise TestError(
                f"This test deliberately requires initial Mode=Standby "
                f"to reproduce the successful manual run. "
                f"COSCON currently reports Mode={initial.mode}."
            )

        gauge = XGS600(
            args.xgs_port,
            args.xgs_baud,
            args.xgs_timeout,
            logger,
        )

        show_preflight(args)
        typed = input(
            f'Type exactly "{CONFIRMATION_PHRASE}" to begin the '
            "verified Standby conditioning and authorize one later "
            "SwitchToOperate command:\n> "
        ).strip()

        if typed != CONFIRMATION_PHRASE:
            logger.result = "cancelled"
            logger.reason = "Cancelled before conditioning and activation."
            logger.add(logger.reason)
            return 2

        run_standby_conditioning(
            client,
            gauge,
            logger,
            args,
        )

        status, monitor, pressure = read_full_snapshot(
            client,
            gauge,
            logger,
            args,
            phase="FINAL_PREFLIGHT",
        )
        if status.mode.lower() != "standby":
            raise TestError(
                f"Expected Standby immediately before activation; "
                f"got {status.mode}."
            )

        capture_diagnostics(client, logger, "BEFORE VALIDATION")

        validate_command = (
            f"ValidateOperateTarget Emission={args.emission:.6e} "
            f"Energy={args.energy:.6g}"
        )
        validate_reply = client.send(validate_command)
        if "OK" not in validate_reply.upper():
            raise TestError(
                f"Unexpected ValidateOperateTarget reply: {validate_reply}"
            )

        status = get_status(client)
        require_interlock_ok(status, "immediately before SwitchToOperate")
        if status.mode.lower() != "standby":
            raise TestError(
                f"Mode changed after validation: {status.mode}."
            )

        pressure = gauge.read_mbar()
        logger.add(f"Final pre-Operate pressure: {pressure:.6e} mbar")
        if not check_pressure(
            pressure,
            args,
            context="final pre-Operate",
        ):
            raise PressureFault(
                "Final pre-Operate pressure is outside the normal window."
            )

        operate_command = (
            f"SwitchToOperate Emission={args.emission:.6e} "
            f"Energy={args.energy:.6g}"
        )

        logger.add(
            "Sending SwitchToOperate once. It will never be resent blindly."
        )
        activation_requested = True

        try:
            operate_reply = client.send(operate_command)
            if "OK" not in operate_reply.upper():
                logger.add(
                    "Unexpected SwitchToOperate reply; polling state "
                    f"without resending: {operate_reply}"
                )
        except CommunicationFault as exc:
            logger.add(
                f"SwitchToOperate reply was not received: {exc}. "
                "The command will not be resent; polling state instead."
            )

        wait_for_operating(
            client,
            gauge,
            logger,
            args,
        )

        wait_for_stable_output(
            client,
            gauge,
            logger,
            args,
        )

        capture_diagnostics(client, logger, "STABLE OPERATING CONFIRMED")

        run_operating_hold(
            client,
            gauge,
            logger,
            args,
        )

        capture_diagnostics(client, logger, "END OF OPERATING HOLD")

        logger.add("Requesting Standby once.")
        try:
            standby_reply = client.send("SwitchToStandby")
            if "OK" not in standby_reply.upper():
                logger.add(
                    "Unexpected SwitchToStandby reply; polling state "
                    f"without resending: {standby_reply}"
                )
        except CommunicationFault as exc:
            logger.add(
                f"SwitchToStandby reply was not received: {exc}. "
                "Polling state without resending."
            )

        final = wait_for_mode(
            client,
            {"Standby", "Off"},
            args.standby_timeout,
            args.poll,
            allow_bad_interlock=False,
        )

        logger.final_mode = final.mode
        logger.final_interlock = final.interlock

        if final.mode.lower() == "standby":
            standby_confirmed = True
        else:
            off_confirmed = True

        capture_diagnostics(client, logger, "FINAL SAFE STATE")

        if standby_confirmed:
            print(
                "\nCOSCON is now in Standby.\n"
                "Close the manual argon leak valve now.\n"
            )
            typed = input(
                f'Type exactly "{VALVE_CLOSED_PHRASE}" after physically '
                "closing the argon valve:\n> "
            ).strip()

            if typed != VALVE_CLOSED_PHRASE:
                logger.add(
                    "WARNING: argon-valve closure was not confirmed in "
                    "the test console."
                )
            else:
                logger.add("Operator confirmed the argon valve is closed.")

        logger.result = "success"
        logger.reason = (
            "UDP activation reached stable measured 2250 V / 10 mA, "
            f"remained stable for {args.hold:.1f} s, and returned to "
            f"Mode={final.mode} without a reported device error."
        )
        logger.add("SUCCESS: " + logger.reason)
        return 0

    except KeyboardInterrupt:
        logger.result = "interrupted"
        logger.reason = "Keyboard interrupt received."
        logger.add(logger.reason)
        return 130

    except PressureFault as exc:
        logger.result = "pressure_safety_stop"
        logger.reason = str(exc)
        logger.add("PRESSURE SAFETY STOP: " + logger.reason)
        capture_diagnostics(client, logger, "PRESSURE SAFETY STOP")
        return 3

    except DeviceFault as exc:
        logger.result = "device_fault"
        logger.reason = str(exc)
        logger.add("DEVICE FAULT: " + logger.reason)
        capture_diagnostics(client, logger, "DEVICE FAULT")
        return 4

    except CommunicationFault as exc:
        logger.result = "communication_fault"
        logger.reason = str(exc)
        logger.add("COMMUNICATION FAULT: " + logger.reason)
        return 5

    except Exception as exc:
        logger.result = "failure"
        logger.reason = str(exc)
        logger.add("ERROR: " + logger.reason)
        return 1

    finally:
        if activation_requested and not (standby_confirmed or off_confirmed):
            logger.add("Failsafe path activated.")
            recovered_standby, recovered_off = safe_stop(
                client,
                logger,
                args,
            )
            standby_confirmed = standby_confirmed or recovered_standby
            off_confirmed = off_confirmed or recovered_off

        if gauge is not None:
            gauge.close()

        logger.close_and_write_summary(
            args=args,
            activation_requested=activation_requested,
            standby_confirmed=standby_confirmed,
            off_confirmed=off_confirmed,
        )


if __name__ == "__main__":
    sys.exit(main())
