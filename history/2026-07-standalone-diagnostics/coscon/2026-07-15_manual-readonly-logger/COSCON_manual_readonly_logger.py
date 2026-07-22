#!/usr/bin/env python3
"""
COSCON IS manual-process read-only logger
=========================================

Purpose:
    Record what the COSCON actually does while an operator performs the normal
    sputtering sequence manually from the web interface or the usual control
    software.

This program is strictly read-only. It can send only:
    Info
    GetStatus
    GetTargetValues
    GetMonitorValues
    GetDiagnosticValues

It cannot send Degas, Operate, Standby, Off, Reset, network or preset commands.

Outputs:
    - raw timestamped communication log
    - COSCON snapshot CSV
    - high-frequency XGS600 pressure CSV
    - final text summary

Stop:
    Return to this console and press ENTER, or press Ctrl+C.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import socket
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError as exc:
    raise SystemExit(
        "pyserial is required for pressure logging. Run this file from the "
        "project .venv or install it with: python -m pip install pyserial"
    ) from exc


DEFAULT_IP = "192.168.236.186"
DEFAULT_UDP_PORT = 2005
DEFAULT_UDP_TIMEOUT_S = 2.0

DEFAULT_XGS_PORT = "COM6"
DEFAULT_XGS_BAUD = 9600
DEFAULT_XGS_TIMEOUT_S = 1.0
DEFAULT_PRESSURE_INTERVAL_S = 0.5

ALLOWED_COMMANDS = {
    "Info",
    "GetStatus",
    "GetTargetValues",
    "GetMonitorValues",
    "GetDiagnosticValues",
}

COMMAND_SCHEDULE_S = {
    "GetStatus": 0.40,
    "GetMonitorValues": 0.65,
    "GetDiagnosticValues": 1.40,
    "GetTargetValues": 1.80,
}

START_PHRASE = "START READ ONLY LOGGER"


class LoggerError(RuntimeError):
    pass


@dataclass
class Snapshot:
    timestamp_iso: str = ""
    elapsed_s: float = 0.0
    triggering_command: str = ""
    reply: str = ""

    mode: str = ""
    interlock: str = ""
    details: str = ""

    target_emission_A: Optional[float] = None
    target_energy_V: Optional[float] = None

    measured_energy_V: Optional[float] = None
    filament_current_A: Optional[float] = None
    measured_emission_A: Optional[float] = None

    energy_current_A: Optional[float] = None
    filament_voltage_V: Optional[float] = None
    anode_voltage_V: Optional[float] = None
    repeller_voltage_V: Optional[float] = None
    temperature_hv_C: Optional[float] = None
    temperature_em_C: Optional[float] = None

    latest_pressure_mbar: Optional[float] = None
    pressure_age_s: Optional[float] = None


class ThreadSafeRawLog:
    def __init__(self, path: Path, start_monotonic: float) -> None:
        self.path = path
        self.start_monotonic = start_monotonic
        self.lock = threading.Lock()
        self.handle = path.open("w", encoding="utf-8", buffering=1)

    def write(self, message: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        elapsed = time.monotonic() - self.start_monotonic
        line = f"[{stamp}] [{elapsed:10.3f} s] {message}\n"
        with self.lock:
            self.handle.write(line)
        print(line, end="")

    def close(self) -> None:
        with self.lock:
            self.handle.close()


class LatestPressure:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: Optional[float] = None
        self.monotonic_time: Optional[float] = None

    def update(self, value: float) -> None:
        with self.lock:
            self.value = value
            self.monotonic_time = time.monotonic()

    def get(self) -> tuple[Optional[float], Optional[float]]:
        with self.lock:
            if self.value is None or self.monotonic_time is None:
                return None, None
            return self.value, time.monotonic() - self.monotonic_time


class SummaryTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode_counts: Counter[str] = Counter()
        self.mode_first: dict[str, float] = {}
        self.mode_last: dict[str, float] = {}
        self.errors: list[tuple[float, str]] = []

        self.max_energy: tuple[float, float] = (-math.inf, 0.0)
        self.max_emission: tuple[float, float] = (-math.inf, 0.0)
        self.max_filament: tuple[float, float] = (-math.inf, 0.0)

        self.target_energy: Optional[float] = None
        self.target_emission: Optional[float] = None

        self.pressure_min = math.inf
        self.pressure_max = -math.inf
        self.snapshot_count = 0

    def update_snapshot(self, snapshot: Snapshot) -> None:
        with self.lock:
            self.snapshot_count += 1
            elapsed = snapshot.elapsed_s

            if snapshot.mode:
                self.mode_counts[snapshot.mode] += 1
                self.mode_first.setdefault(snapshot.mode, elapsed)
                self.mode_last[snapshot.mode] = elapsed
                if snapshot.mode.lower() == "error":
                    entry = (elapsed, snapshot.details)
                    if entry not in self.errors:
                        self.errors.append(entry)

            if snapshot.measured_energy_V is not None:
                if snapshot.measured_energy_V > self.max_energy[0]:
                    self.max_energy = (snapshot.measured_energy_V, elapsed)

            if snapshot.measured_emission_A is not None:
                if snapshot.measured_emission_A > self.max_emission[0]:
                    self.max_emission = (snapshot.measured_emission_A, elapsed)

            if snapshot.filament_current_A is not None:
                if snapshot.filament_current_A > self.max_filament[0]:
                    self.max_filament = (snapshot.filament_current_A, elapsed)

            if snapshot.target_energy_V is not None:
                self.target_energy = snapshot.target_energy_V
            if snapshot.target_emission_A is not None:
                self.target_emission = snapshot.target_emission_A

            if snapshot.latest_pressure_mbar is not None:
                self.pressure_min = min(
                    self.pressure_min, snapshot.latest_pressure_mbar
                )
                self.pressure_max = max(
                    self.pressure_max, snapshot.latest_pressure_mbar
                )

    def update_pressure(self, value: float) -> None:
        with self.lock:
            self.pressure_min = min(self.pressure_min, value)
            self.pressure_max = max(self.pressure_max, value)


def parse_number_fields(reply: str) -> dict[str, float]:
    pairs = re.findall(
        r"\b([A-Za-z][A-Za-z0-9]*)="
        r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)",
        reply,
    )
    result: dict[str, float] = {}
    for key, value in pairs:
        try:
            result[key] = float(value)
        except ValueError:
            continue
    return result


def parse_status(reply: str) -> tuple[str, str, str]:
    mode_match = re.search(r"\bMode=([^\s]+)", reply, re.IGNORECASE)
    interlock_match = re.search(r"\bInterlock=([^\s]+)", reply, re.IGNORECASE)
    details_match = re.search(r'Details="([^"]*)"', reply, re.IGNORECASE)

    mode = mode_match.group(1) if mode_match else ""
    interlock = interlock_match.group(1) if interlock_match else ""
    details = details_match.group(1) if details_match else ""
    return mode, interlock, details


class CosconReadOnly:
    def __init__(
        self,
        ip: str,
        port: int,
        timeout_s: float,
        raw_log: ThreadSafeRawLog,
    ) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self.raw_log = raw_log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout_s)

    def close(self) -> None:
        self.sock.close()

    def send(self, command: str) -> str:
        if command not in ALLOWED_COMMANDS:
            raise LoggerError(f"Blocked non-read-only command: {command!r}")

        payload = (command + "\r").encode("ascii")
        self.raw_log.write(f"COSCON -> {command}")
        self.sock.sendto(payload, (self.ip, self.port))

        data, sender = self.sock.recvfrom(8192)
        if sender[0] != self.ip:
            raise LoggerError(
                f"Unexpected COSCON reply source: {sender[0]}:{sender[1]}"
            )

        reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
        self.raw_log.write(f"COSCON <- {reply}")
        if not reply:
            raise LoggerError(f"Empty reply to {command!r}")
        return reply


class XGS600PressureReader:
    COMMAND = b"#0002USYNTH\r"
    NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

    def __init__(
        self,
        port: str,
        baud: int,
        timeout_s: float,
        raw_log: ThreadSafeRawLog,
    ) -> None:
        self.raw_log = raw_log
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
            raise LoggerError(
                f"Could not open XGS600 on {port}: {exc}. "
                "Close Phase 2 and any other program using this COM port."
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
            self.raw_log.write(f"XGS600 cleanup warning: {exc}")

    def read_mbar(self) -> float:
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
        cleaned = text.lstrip(">").strip()

        if cleaned.lower() in {"nan", "+nan", "-nan", ""}:
            raise LoggerError(f"XGS600 returned unavailable pressure: {text!r}")

        match = self.NUMBER_RE.search(cleaned)
        if not match:
            raise LoggerError(f"Could not parse XGS600 reply: {text!r}")

        value = float(match.group(0))
        if not math.isfinite(value) or value <= 0:
            raise LoggerError(f"Invalid pressure value: {value!r}")
        return value


def update_snapshot_from_reply(
    snapshot: Snapshot,
    command: str,
    reply: str,
) -> None:
    if command == "GetStatus":
        mode, interlock, details = parse_status(reply)
        snapshot.mode = mode
        snapshot.interlock = interlock
        snapshot.details = details
        return

    fields = parse_number_fields(reply)

    if command == "GetTargetValues":
        snapshot.target_emission_A = fields.get("Emission")
        snapshot.target_energy_V = fields.get("Energy")

    elif command == "GetMonitorValues":
        snapshot.measured_energy_V = fields.get("VEnergy")
        snapshot.filament_current_A = fields.get("IFilament")
        snapshot.measured_emission_A = fields.get("IEmission")

    elif command == "GetDiagnosticValues":
        snapshot.energy_current_A = fields.get("IEnergy")
        snapshot.filament_voltage_V = fields.get("VFilament")
        snapshot.anode_voltage_V = fields.get("VAnode")
        snapshot.repeller_voltage_V = fields.get("VRepeller")
        snapshot.temperature_hv_C = fields.get("TemperatureHV")
        snapshot.temperature_em_C = fields.get("TemperatureEM")


def pressure_worker(
    stop_event: threading.Event,
    reader: XGS600PressureReader,
    latest: LatestPressure,
    raw_log: ThreadSafeRawLog,
    pressure_csv_path: Path,
    start_monotonic: float,
    interval_s: float,
    summary: SummaryTracker,
) -> None:
    with pressure_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "timestamp_iso",
                "elapsed_s",
                "pressure_mbar",
                "status",
                "message",
            ],
        )
        writer.writeheader()

        while not stop_event.is_set():
            timestamp = datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            )
            elapsed = time.monotonic() - start_monotonic
            try:
                value = reader.read_mbar()
                latest.update(value)
                summary.update_pressure(value)
                writer.writerow({
                    "timestamp_iso": timestamp,
                    "elapsed_s": f"{elapsed:.3f}",
                    "pressure_mbar": f"{value:.9e}",
                    "status": "OK",
                    "message": "",
                })
                handle.flush()
            except Exception as exc:
                message = str(exc)
                raw_log.write(f"XGS600 ERROR: {message}")
                writer.writerow({
                    "timestamp_iso": timestamp,
                    "elapsed_s": f"{elapsed:.3f}",
                    "pressure_mbar": "",
                    "status": "ERROR",
                    "message": message,
                })
                handle.flush()

            stop_event.wait(interval_s)


def coscon_worker(
    stop_event: threading.Event,
    client: CosconReadOnly,
    latest_pressure: LatestPressure,
    raw_log: ThreadSafeRawLog,
    snapshot_csv_path: Path,
    start_monotonic: float,
    summary: SummaryTracker,
) -> None:
    snapshot = Snapshot()
    next_due = {
        command: time.monotonic()
        for command in COMMAND_SCHEDULE_S
    }

    fieldnames = list(asdict(snapshot).keys())

    with snapshot_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        try:
            client.send("Info")
        except Exception as exc:
            raw_log.write(f"COSCON Info warning: {exc}")

        while not stop_event.is_set():
            now = time.monotonic()
            command = min(next_due, key=next_due.get)

            wait_s = next_due[command] - now
            if wait_s > 0:
                stop_event.wait(min(wait_s, 0.1))
                continue

            try:
                reply = client.send(command)
                update_snapshot_from_reply(snapshot, command, reply)

                pressure, pressure_age = latest_pressure.get()
                snapshot.timestamp_iso = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
                snapshot.elapsed_s = time.monotonic() - start_monotonic
                snapshot.triggering_command = command
                snapshot.reply = reply
                snapshot.latest_pressure_mbar = pressure
                snapshot.pressure_age_s = pressure_age

                row = asdict(snapshot)
                writer.writerow(row)
                handle.flush()
                summary.update_snapshot(snapshot)

                if snapshot.mode.lower() == "error":
                    raw_log.write(
                        f"DEVICE REPORTED Mode=Error: {snapshot.details}"
                    )

            except socket.timeout:
                raw_log.write(f"COSCON TIMEOUT: {command}")
            except Exception as exc:
                raw_log.write(f"COSCON ERROR [{command}]: {exc}")

            next_due[command] = (
                max(next_due[command], time.monotonic())
                + COMMAND_SCHEDULE_S[command]
            )


def write_summary(
    path: Path,
    summary: SummaryTracker,
    start_time: datetime,
    end_time: datetime,
    args: argparse.Namespace,
) -> None:
    duration = (end_time - start_time).total_seconds()

    with summary.lock:
        max_energy = summary.max_energy
        max_emission = summary.max_emission
        max_filament = summary.max_filament
        pressure_min = summary.pressure_min
        pressure_max = summary.pressure_max

        lines = [
            "COSCON MANUAL PROCESS READ-ONLY LOGGER — SUMMARY",
            "================================================",
            f"Start: {start_time.astimezone().isoformat(timespec='seconds')}",
            f"End: {end_time.astimezone().isoformat(timespec='seconds')}",
            f"Duration: {duration:.1f} s",
            f"COSCON: {args.ip}:{args.udp_port}",
            f"Pressure gauge: {args.xgs_port}",
            "",
            "This logger sent read-only queries only.",
            "",
            f"COSCON snapshot rows: {summary.snapshot_count}",
            f"Modes observed: {dict(summary.mode_counts)}",
        ]

        if summary.target_energy is not None:
            lines.append(
                f"Last observed target energy: "
                f"{summary.target_energy:.6g} V"
            )
        if summary.target_emission is not None:
            lines.append(
                f"Last observed target emission: "
                f"{summary.target_emission:.6e} A"
            )

        if max_energy[0] != -math.inf:
            lines.append(
                f"Maximum measured VEnergy: {max_energy[0]:.6g} V "
                f"at {max_energy[1]:.3f} s"
            )
        if max_emission[0] != -math.inf:
            lines.append(
                f"Maximum measured IEmission: {max_emission[0]:.6e} A "
                f"at {max_emission[1]:.3f} s"
            )
        if max_filament[0] != -math.inf:
            lines.append(
                f"Maximum measured IFilament: {max_filament[0]:.6e} A "
                f"at {max_filament[1]:.3f} s"
            )

        if pressure_min != math.inf:
            lines.append(
                f"Pressure range: {pressure_min:.6e} to "
                f"{pressure_max:.6e} mbar"
            )

        lines.extend(["", "Mode timing estimates:"])
        for mode in sorted(summary.mode_first):
            first = summary.mode_first[mode]
            last = summary.mode_last[mode]
            lines.append(
                f"  {mode}: first {first:.3f} s, last {last:.3f} s, "
                f"observations {summary.mode_counts[mode]}"
            )

        lines.extend(["", "Reported device errors:"])
        if summary.errors:
            for elapsed, details in summary.errors:
                lines.append(f"  At {elapsed:.3f} s: {details}")
        else:
            lines.append("  None observed.")

        lines.extend([
            "",
            "Interpretation guide:",
            "- If manual operation also reports HV-Module Energy Overload, "
            "the behavior is not caused by the Python activation command.",
            "- If manual operation remains Operating and VEnergy approaches "
            "the target, the manual control path differs from the automated test.",
            "- Mode=Operating is not sufficient by itself: compare measured "
            "VEnergy and IEmission with their targets and check for later errors.",
        ])

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only COSCON logger for a manually controlled run."
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
    parser.add_argument(
        "--pressure-interval",
        type=float,
        default=DEFAULT_PRESSURE_INTERVAL_S,
    )
    parser.add_argument(
        "--output-dir",
        default="COSCON Manual Logger Reports",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.pressure_interval <= 0:
        raise SystemExit("--pressure-interval must be positive.")

    print(
        "\nCOSCON MANUAL PROCESS — READ-ONLY LOGGER\n"
        "----------------------------------------\n"
        "This program does not control the COSCON. It only records status,\n"
        "targets, measured values, diagnostics and XGS600 pressure.\n"
        "\n"
        "Use only ONE manual control client: the web interface OR SpecsLab.\n"
        "Do not run Phase 2 at the same time.\n"
        "\n"
        "Recommended procedure:\n"
        "1. Put the chamber and COSCON in the normal starting condition.\n"
        "2. Start this logger.\n"
        "3. Perform the usual sputtering sequence manually.\n"
        "4. After the manual process ends or an error appears, return here.\n"
        "5. Press ENTER to stop and save all reports.\n"
    )

    typed = input(
        f'Type exactly "{START_PHRASE}" to start logging, '
        "or press Enter to cancel:\n> "
    ).strip()
    if typed != START_PHRASE:
        print("Cancelled. No commands were sent.")
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_path = output_dir / f"coscon_manual_{stamp}_raw.log"
    snapshot_path = output_dir / f"coscon_manual_{stamp}_snapshots.csv"
    pressure_path = output_dir / f"coscon_manual_{stamp}_pressure.csv"
    summary_path = output_dir / f"coscon_manual_{stamp}_summary.txt"

    start_monotonic = time.monotonic()
    start_datetime = datetime.now().astimezone()
    raw_log = ThreadSafeRawLog(raw_path, start_monotonic)
    stop_event = threading.Event()
    latest_pressure = LatestPressure()
    summary = SummaryTracker()

    client: Optional[CosconReadOnly] = None
    pressure_reader: Optional[XGS600PressureReader] = None
    threads: list[threading.Thread] = []

    try:
        client = CosconReadOnly(
            args.ip,
            args.udp_port,
            args.udp_timeout,
            raw_log,
        )
        pressure_reader = XGS600PressureReader(
            args.xgs_port,
            args.xgs_baud,
            args.xgs_timeout,
            raw_log,
        )

        pressure_thread = threading.Thread(
            target=pressure_worker,
            name="pressure-logger",
            daemon=True,
            args=(
                stop_event,
                pressure_reader,
                latest_pressure,
                raw_log,
                pressure_path,
                start_monotonic,
                args.pressure_interval,
                summary,
            ),
        )
        coscon_thread = threading.Thread(
            target=coscon_worker,
            name="coscon-logger",
            daemon=True,
            args=(
                stop_event,
                client,
                latest_pressure,
                raw_log,
                snapshot_path,
                start_monotonic,
                summary,
            ),
        )
        threads = [pressure_thread, coscon_thread]
        for thread in threads:
            thread.start()

        print(
            "\nLOGGING IS ACTIVE.\n"
            "Perform the process manually now.\n"
            "Return to this window and press ENTER when finished.\n"
        )
        try:
            input()
        except KeyboardInterrupt:
            print("\nCtrl+C received. Stopping logger.")

    except Exception as exc:
        raw_log.write(f"LOGGER STARTUP/MAIN ERROR: {exc}")
        print(f"\nERROR: {exc}")
        return 1

    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5.0)

        if pressure_reader is not None:
            pressure_reader.close()
        if client is not None:
            client.close()

        end_datetime = datetime.now().astimezone()
        write_summary(
            summary_path,
            summary,
            start_datetime,
            end_datetime,
            args,
        )
        raw_log.write("Logger stopped. Reports finalized.")
        raw_log.close()

        print(
            "\nReports saved:\n"
            f"  Summary:   {summary_path.resolve()}\n"
            f"  Snapshots: {snapshot_path.resolve()}\n"
            f"  Pressure:  {pressure_path.resolve()}\n"
            f"  Raw log:   {raw_path.resolve()}\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
