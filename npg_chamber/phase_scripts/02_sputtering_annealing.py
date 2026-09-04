"""
Automated Sputtering-Annealings Controller
==========================================

Phase 02 now uses direct COSCON IS UDP control and an operator-focused dashboard.
The COSCON web page is neither embedded nor required.

Automated actions
-----------------
- Complete COSCON Degas in cycle 1 and wait for natural Standby.
- Validate and apply the 10 mA / 2250 V sputtering target.
- Confirm actual Operating mode, energy, emission, interlock and pressure.
- Run the sputtering countdown with continuous safety supervision.
- Return COSCON to Standby before the leak valve is closed.
- Write and verify the oven PID setpoint, wait for temperature and run anneal holds.

Manual actions
--------------
- Confirm the sputter-gun cable/electronics preflight.
- Open and close the argon leak valve when requested.
- Remain present and use Abort / Safe Stop when necessary.

The dashboard shows only currently usable operator buttons, live COSCON/chamber/
oven telemetry, current-step timing, run elapsed time, known timed work remaining,
and a visual workflow for the active cycle.
"""


from __future__ import annotations

import csv
import math
import multiprocessing as mp
import os
import queue
import re
import socket
import sys
import time
import traceback


def _resolve_phase_data_parent(phase_folder_name: str) -> str:
    """Return the project Data Samples folder for this workflow.

    This only changes where output files are saved. It does not change the
    experimental control logic, parameters, setpoints, ramps, or safety checks.
    """
    env_phase_dir = os.environ.get("NPG_CHAMBER_PHASE_DATA_DIR")
    if env_phase_dir:
        os.makedirs(env_phase_dir, exist_ok=True)
        return env_phase_dir

    current = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    candidate = current
    while True:
        if (
            os.path.isfile(os.path.join(candidate, "pyproject.toml"))
            and os.path.isdir(os.path.join(candidate, "npg_chamber"))
        ):
            data_dir = os.path.join(candidate, "Data Samples", phase_folder_name)
            os.makedirs(data_dir, exist_ok=True)
            return data_dir
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent

    fallback = os.path.join(current, "Data Samples", phase_folder_name)
    os.makedirs(fallback, exist_ok=True)
    return fallback
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Callable

from npg_chamber.common.pressure_alarm import PressureEmergencyAlarm
from npg_chamber.common.paths import create_numbered_run_dir

from npg_chamber.config.run_parameters import (
    apply_overrides_to_object,
    load_phase_overrides,
    write_effective_parameters,
)

RUN_AUTOMATION_OVERRIDES = load_phase_overrides("sputter")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    import serial
except ImportError as exc:
    raise SystemExit(
        "pyserial is required. Install it with: pip install pyserial colorama pywebview"
    ) from exc

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:
    class _Dummy:
        def __getattr__(self, _name: str) -> str:
            return ""

    Fore = Style = _Dummy()  # type: ignore

    def colorama_init(*_args, **_kwargs):
        return None


# =============================================================================
# USER CONFIGURATION
# =============================================================================

@dataclass
class RunConfig:
    run_name: str = "Run"

    # Chamber workflow
    cycles: int = 3
    start_without_degassing: bool = False
    sputter_minutes: int = 20
    anneal_target_c: float = 620.0
    anneal_hold_minutes: int = 10
    anneal_reset_c: float = 0.0
    abort_reset_c: float = 0.0

    # COSCON UDP automation
    coscon_ip: str = "192.168.236.186"
    coscon_udp_port: int = 2005
    coscon_udp_timeout_s: float = 2.0
    coscon_window_title: str = "Phase 02 · Automated Sputtering-Annealing"
    coscon_window_width: int = 1420
    coscon_window_height: int = 920
    degas_timeout_minutes: int = 25
    standby_conditioning_s: int = 60
    operate_transition_timeout_s: int = 35
    coscon_activation_overload_retries: int = 1
    coscon_activation_recovery_wait_s: float = 8.0
    coscon_activation_reset_retries: int = 1
    coscon_reset_reconnect_timeout_s: float = 60.0
    coscon_reset_safe_samples: int = 3
    coscon_reset_safe_sample_interval_s: float = 2.0
    coscon_post_reset_conditioning_s: float = 60.0
    coscon_energy_v: float = 2250.0
    coscon_emission_a: float = 0.010
    coscon_energy_tolerance_v: float = 50.0
    coscon_emission_tolerance_a: float = 0.001
    # A single emission spike raises a warning and is rechecked. The run is
    # aborted only after this many consecutive out-of-tolerance measurements.
    coscon_emission_fault_samples: int = 3
    coscon_emission_recheck_s: float = 0.5
    coscon_stable_samples: int = 5
    pressure_min_mbar: float = 1.0e-5
    pressure_emergency_mbar: float = 1.0e-4

    # Pressure guidance
    target_ar_pressure_mbar: float = 2.0e-5
    pressure_warning_mbar: float = 3.0e-5

    # Ports
    xgs600_port: str = "COM6"
    xgs600_baud: int = 9600

    pid_port: str = "COM9"
    pid_baud: int = 9600
    pid_address: str = "00"

    keysight_port: Optional[str] = "COM17"
    keysight_baud: int = 9600

    # Monitoring
    monitor_period_s: float = 2.0
    temperature_reach_tolerance_c: float = 5.0
    temperature_stable_duration_s: float = 30.0
    pause_hold_outside_temperature_band: bool = True

    # Abort behaviour
    try_reset_pid_on_abort: bool = True


# The COSCON firmware needs a short quiet interval after target validation.  The
# interval is deliberately fixed here: it is part of the controller handoff,
# not an operator-tunable process parameter.
COSCON_ACTIVATION_QUIET_S = 1.5

# Manual dashboard actions must never leave the parent process in an
# unbounded queue read.  The timeout is only a watchdog: it does not act on the
# hardware or replace the operator's confirmation.
UI_TOKEN_POLL_S = 0.25
OPERATOR_PROMPT_TIMEOUT_S = 15 * 60


@dataclass
class MonitorState:
    stage: str = "INIT"
    cycle: int = 0
    pressure_mbar: Optional[float] = None
    oven_pv_c: Optional[float] = None
    oven_sv_c: Optional[float] = None
    keysight_voltage_v: Optional[float] = None
    keysight_current_a: Optional[float] = None
    coscon_mode: str = ""
    coscon_interlock: str = ""
    coscon_details: str = ""
    coscon_energy_v: Optional[float] = None
    coscon_emission_a: Optional[float] = None
    coscon_filament_a: Optional[float] = None
    coscon_energy_current_a: Optional[float] = None
    coscon_anode_voltage_v: Optional[float] = None
    coscon_repeller_voltage_v: Optional[float] = None
    coscon_emission_bad_samples: int = 0
    phase_remaining_s: Optional[int] = None
    phase_total_s: Optional[int] = None
    phase_timer_label: str = ""
    last_error: Optional[str] = None
    last_update: Optional[datetime] = None

    def snapshot(self) -> dict:
        return {
            "stage": self.stage,
            "cycle": self.cycle,
            "pressure_mbar": self.pressure_mbar,
            "oven_pv_c": self.oven_pv_c,
            "oven_sv_c": self.oven_sv_c,
            "keysight_voltage_v": self.keysight_voltage_v,
            "keysight_current_a": self.keysight_current_a,
            "coscon_mode": self.coscon_mode,
            "coscon_interlock": self.coscon_interlock,
            "coscon_details": self.coscon_details,
            "coscon_energy_v": self.coscon_energy_v,
            "coscon_emission_a": self.coscon_emission_a,
            "coscon_filament_a": self.coscon_filament_a,
            "coscon_energy_current_a": self.coscon_energy_current_a,
            "coscon_anode_voltage_v": self.coscon_anode_voltage_v,
            "coscon_repeller_voltage_v": self.coscon_repeller_voltage_v,
            "coscon_emission_bad_samples": self.coscon_emission_bad_samples,
            "phase_remaining_s": self.phase_remaining_s,
            "phase_total_s": self.phase_total_s,
            "phase_timer_label": self.phase_timer_label,
            "last_error": self.last_error or "",
            "last_update": self.last_update.isoformat() if self.last_update else "",
        }


# =============================================================================
# UTILS
# =============================================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def banner(text: str) -> None:
    line = "=" * len(text)
    print(f"\n{Fore.CYAN}{line}\n{text}\n{line}{Style.RESET_ALL}")


def info(text: str) -> None:
    print(f"{Fore.GREEN}[{now_str()}] {text}{Style.RESET_ALL}")


def warn(text: str) -> None:
    print(f"{Fore.YELLOW}[{now_str()}] WARNING: {text}{Style.RESET_ALL}")


def fmt_opt(value: Optional[float], fmt: str) -> str:
    return "nan" if value is None else format(value, fmt)


def safe_float_from_text(text: str) -> Optional[float]:
    matches = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)
    if not matches:
        return None
    try:
        return float(matches[0])
    except ValueError:
        return None


def reset_serial_buffers_and_close(ser, label: str) -> None:
    """Best-effort PC-side buffer reset and close for phase handoff."""

    try:
        if getattr(ser, "is_open", False):
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            try:
                ser.reset_output_buffer()
            except Exception:
                pass
    finally:
        try:
            ser.close()
            info(f"Serial cleanup: closed {label}.")
        except Exception as exc:
            warn(f"Serial cleanup warning for {label}: {exc}")


def make_short_output_dir(base_name: str) -> str:
    """
    Create the next numbered output folder under:
        Data Samples/Sputtering-Annealing Data/

    Every phase uses the same naming pattern:
        "Sputtering-Annealing <sample> data NN"
    """
    base_root = _resolve_phase_data_parent("Sputtering-Annealing Data")
    return str(create_numbered_run_dir("sputter", base_name, parent=base_root))



# =============================================================================
# LOGGER
# =============================================================================

class DataLogger:
    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        self.csv_path = os.path.join(self.output_dir, "sputter_anneal_log.csv")
        self._fh = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(
            [
                "timestamp",
                "cycle",
                "stage",
                "pressure_mbar",
                "oven_pv_c",
                "oven_sv_c",
                "keysight_voltage_v",
                "keysight_current_a",
                "coscon_mode",
                "coscon_interlock",
                "coscon_details",
                "coscon_energy_v",
                "coscon_emission_a",
                "coscon_filament_a",
                "coscon_energy_current_a",
                "coscon_anode_voltage_v",
                "coscon_repeller_voltage_v",
                "coscon_emission_bad_samples",
                "phase_remaining_s",
                "phase_total_s",
                "phase_timer_label",
                "note",
            ]
        )
        self._fh.flush()

    def log_snapshot(self, snap: dict, note: str = "") -> None:
        self._writer.writerow(
            [
                now_str(),
                snap["cycle"],
                snap["stage"],
                snap["pressure_mbar"],
                snap["oven_pv_c"],
                snap["oven_sv_c"],
                snap["keysight_voltage_v"],
                snap["keysight_current_a"],
                snap["coscon_mode"],
                snap["coscon_interlock"],
                snap["coscon_details"],
                snap["coscon_energy_v"],
                snap["coscon_emission_a"],
                snap["coscon_filament_a"],
                snap["coscon_energy_current_a"],
                snap["coscon_anode_voltage_v"],
                snap["coscon_repeller_voltage_v"],
                snap["coscon_emission_bad_samples"],
                snap["phase_remaining_s"],
                snap["phase_total_s"],
                snap["phase_timer_label"],
                note,
            ]
        )
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# =============================================================================
# DEVICES
# =============================================================================

class XGS600Gauge:
    def __init__(self, port: str, baud: int, timeout: float = 1.0) -> None:
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)

    def read_pressure_mbar(self) -> float:
        self.ser.write(b"#0002USYNTH\r")
        time.sleep(0.1)
        message = self.ser.read(self.ser.in_waiting or 100).decode(errors="ignore").strip()
        message = message.lstrip(">")
        # Temporary XGS600/PC issue: the controller can return literal NaN.
        # Treat that as a valid unavailable pressure reading so the workflow and
        # COSCON/SPECS guidance can continue while the operator checks pressure manually.
        if message.strip().lower() in {"nan", "+nan", "-nan"}:
            return float("nan")
        value = safe_float_from_text(message)
        if value is None:
            raise RuntimeError(f"Could not parse XGS600 pressure from: {message!r}")
        return value

    def close(self) -> None:
        reset_serial_buffers_and_close(self.ser, "XGS600 pressure port")


class OvenPID:
    EOT = b"\x04"
    ENQ = b"\x05"
    STX = b"\x02"
    ETX = b"\x03"
    ACK = b"\x06"
    NAK = b"\x15"

    def __init__(self, port: str, baud: int, address: str = "00", timeout: float = 1.0) -> None:
        self.address = address
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)
        # The monitor thread reads M1/S1 while the main workflow writes S1.
        # Keep every PID serial transaction serialized so read/write frames cannot overlap.
        self.lock = threading.RLock()

    def _xor_bcc(self, identifier_plus_data_plus_etx: bytes) -> bytes:
        x = 0
        for b in identifier_plus_data_plus_etx:
            x ^= b
        return bytes([x])

    def _read_identifier_raw(self, identifier: str, wait_s: float = 0.15) -> bytes:
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(self.EOT)
            time.sleep(0.05)
            self.ser.write(self.address.encode("ascii") + identifier.encode("ascii") + self.ENQ)
            time.sleep(wait_s)
            return self.ser.read(self.ser.in_waiting or 64)

    def _parse_frame(self, raw: bytes) -> dict:
        if raw == b"":
            return {"status": "NO_RESPONSE", "raw": raw}
        if raw == self.NAK:
            return {"status": "NAK", "raw": raw}
        if raw == self.ACK:
            return {"status": "ACK", "raw": raw}
        if len(raw) >= 5 and raw[0:1] == self.STX:
            try:
                etx_index = raw.index(self.ETX)
            except ValueError:
                return {"status": "UNKNOWN_FRAME", "raw": raw, "decoded": raw.decode(errors="ignore")}
            core = raw[1:etx_index]
            if len(core) < 2:
                return {"status": "SHORT_FRAME", "raw": raw, "decoded": raw.decode(errors="ignore")}
            ident = core[:2].decode(errors="ignore")
            data = core[2:].decode(errors="ignore")
            return {"status": "DATA", "raw": raw, "decoded": raw.decode(errors="ignore"), "ident": ident, "data": data}
        return {"status": "UNKNOWN", "raw": raw, "decoded": raw.decode(errors="ignore")}

    def _parse_numeric_ascii(self, data: str) -> Optional[float]:
        s = data.strip()
        if s == "":
            return None
        try:
            return float(s)
        except ValueError:
            pass
        allowed = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
        if allowed in ("", "-", ".", "-."):
            return None
        try:
            return float(allowed)
        except ValueError:
            return None

    def _read_value(self, identifier: str) -> tuple[dict, Optional[float]]:
        parsed = self._parse_frame(self._read_identifier_raw(identifier))
        value = None
        if parsed.get("status") == "DATA":
            value = self._parse_numeric_ascii(parsed.get("data", ""))
        return parsed, value

    def read_process_value_c(self) -> float:
        parsed, value = self._read_value("M1")
        if parsed.get("status") != "DATA" or value is None:
            raise RuntimeError(f"Could not read M1/PV from PID: {parsed!r}")
        return value

    def read_setpoint_c(self) -> float:
        parsed, value = self._read_value("S1")
        if parsed.get("status") != "DATA" or value is None:
            raise RuntimeError(f"Could not read S1/SV from PID: {parsed!r}")
        return value

    def _format_target_like_current_data(self, current_data: str, target_value: float) -> str:
        template = current_data.strip()
        if not template:
            raise ValueError("No current S1 data available to infer format.")
        negative = template.startswith("-")
        body = template[1:] if negative else template

        if "." in body:
            left, right = body.split(".", 1)
            decimals = len(right)
            width_left = len(left)
            scaled = round(target_value, decimals)
            fmt = f"{{:0{width_left}.{decimals}f}}"
            text = fmt.format(scaled)
            if negative and target_value < 0 and not text.startswith("-"):
                text = "-" + text
            return text

        width = len(body)
        if not float(target_value).is_integer():
            raise ValueError(
                f"S1 currently has no decimal point ('{template}'). Use an integer target or adapt the formatter."
            )
        ivalue = int(round(target_value))
        sign = "-" if ivalue < 0 else ""
        digits = str(abs(ivalue)).zfill(width)
        return sign + digits

    def write_setpoint_c(self, target_c: float, verify: bool = True) -> tuple[bool, str]:
        with self.lock:
            s1_before, current_sv = self._read_value("S1")
            if s1_before.get("status") != "DATA" or current_sv is None:
                raise RuntimeError(f"Could not read S1 before writing: {s1_before!r}")

            data_text = self._format_target_like_current_data(s1_before["data"], target_c)
            body = b"S1" + data_text.encode("ascii") + self.ETX
            bcc = self._xor_bcc(body)
            frame = self.EOT + self.address.encode("ascii") + self.STX + body + bcc

            self.ser.reset_input_buffer()
            self.ser.write(frame)
            time.sleep(0.2)
            reply = self.ser.read(1)

            if reply != self.ACK:
                status = "NO_RESPONSE" if reply == b"" else ("NAK" if reply == self.NAK else repr(reply))
                return False, status

            if not verify:
                return True, "ACK"

            time.sleep(0.2)
            s1_after, sv_after = self._read_value("S1")
            if s1_after.get("status") == "DATA" and sv_after is not None and abs(sv_after - target_c) < 0.51:
                return True, "ACK+VERIFY_OK"
            if s1_after.get("status") == "DATA" and sv_after is not None:
                return False, f"ACK_BUT_SV={sv_after}"
            return False, f"ACK_BUT_REREAD_FAILED:{s1_after}"

    def close(self) -> None:
        reset_serial_buffers_and_close(self.ser, "Oven PID port")


class KeysightSupply:
    def __init__(self, port: str, baud: int, timeout: float = 1.0) -> None:
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)

    def read_voltage_current(self) -> tuple[Optional[float], Optional[float]]:
        self.ser.write(b"system:remote\n")
        time.sleep(0.1)
        self.ser.write(b"measure:voltage?\n")
        time.sleep(0.1)
        measured_voltage = self.ser.readline().decode(errors="ignore").strip()
        self.ser.write(b"measure:current?\n")
        time.sleep(0.1)
        measured_current = self.ser.readline().decode(errors="ignore").strip()
        self.ser.write(b"system:local\n")
        time.sleep(0.1)
        return safe_float_from_text(measured_voltage), safe_float_from_text(measured_current)

    def close(self) -> None:
        reset_serial_buffers_and_close(self.ser, "Keysight power-supply port")



@dataclass
class CosconStatus:
    mode: str
    interlock: str
    details: str
    raw: str


@dataclass
class CosconMonitor:
    energy_v: float
    filament_a: float
    emission_a: float
    raw: str


@dataclass
class CosconDiagnostics:
    energy_current_a: Optional[float]
    anode_voltage_v: Optional[float]
    repeller_voltage_v: Optional[float]
    raw: str


class CosconUDP:
    """Strict COSCON IS UDP client used by automated Phase 02.

    Commands are sent as plain ASCII terminated by one real carriage return.
    A status reply containing ``Mode=Error`` is still a valid GetStatus reply;
    only command-level ``ERROR`` replies are treated as rejected commands.
    """

    EXACT = {
        "Info",
        "GetStatus",
        "GetTargetValues",
        "GetMonitorValues",
        "GetDiagnosticValues",
        "Uptime",
        "SwitchToDegas",
        "SwitchToStandby",
        "SwitchToOff",
        "Reset",
    }
    VALIDATE_RE = re.compile(
        r"^ValidateOperateTarget Emission=[-+0-9.eE]+ Energy=[-+0-9.eE]+$"
    )
    OPERATE_RE = re.compile(
        r"^SwitchToOperate Emission=[-+0-9.eE]+ Energy=[-+0-9.eE]+$"
    )
    MODE_RE = re.compile(r"\bMode=([^\s]+)", re.I)
    INTERLOCK_RE = re.compile(r"\bInterlock=([^\s]+)", re.I)
    DETAILS_RE = re.compile(r'Details="([^"]*)"', re.I)
    NUMBER_RE = re.compile(
        r"\b([A-Za-z][A-Za-z0-9_]*)="
        r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
    )

    def __init__(self, ip: str, port: int, timeout_s: float = 2.0) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self.lock = threading.RLock()

    def _allowed(self, command: str) -> bool:
        return (
            command in self.EXACT
            or bool(self.VALIDATE_RE.fullmatch(command))
            or bool(self.OPERATE_RE.fullmatch(command))
        )

    def send(self, command: str, timeout_s: Optional[float] = None) -> str:
        if not self._allowed(command):
            raise RuntimeError(f"Blocked COSCON command: {command!r}")

        effective_timeout_s = self.timeout_s if timeout_s is None else max(0.05, float(timeout_s))
        with self.lock:
            payload = (command + "\r").encode("ascii")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(effective_timeout_s)
                sock.sendto(payload, (self.ip, self.port))
                try:
                    data, sender = sock.recvfrom(8192)
                except socket.timeout as exc:
                    raise RuntimeError(
                        f"No COSCON reply to {command!r} within "
                        f"{effective_timeout_s:.2f} s."
                    ) from exc

            if sender[0] != self.ip:
                raise RuntimeError(
                    f"Unexpected COSCON reply source: {sender[0]}:{sender[1]}"
                )

            reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
            if not reply:
                raise RuntimeError(f"Empty COSCON reply to {command!r}.")

            command_name = command.split()[0]
            command_error = re.compile(
                rf"^{re.escape(command_name)}\s+ERROR\b",
                re.I,
            )
            if command_error.search(reply) or re.match(r"^ERROR\b", reply, re.I):
                raise RuntimeError(f"COSCON rejected {command!r}: {reply}")

            return reply

    def status(self) -> CosconStatus:
        raw = self.send("GetStatus")
        mode_match = self.MODE_RE.search(raw)
        interlock_match = self.INTERLOCK_RE.search(raw)
        details_match = self.DETAILS_RE.search(raw)
        if not mode_match or not interlock_match:
            raise RuntimeError(f"Could not parse COSCON status: {raw}")
        return CosconStatus(
            mode=mode_match.group(1),
            interlock=interlock_match.group(1),
            details=details_match.group(1) if details_match else "",
            raw=raw,
        )

    def monitor(self) -> CosconMonitor:
        raw = self.send("GetMonitorValues")
        fields = {key: float(value) for key, value in self.NUMBER_RE.findall(raw)}
        for key in ("VEnergy", "IFilament", "IEmission"):
            if key not in fields:
                raise RuntimeError(
                    f"Missing {key} in COSCON monitor reply: {raw}"
                )
        return CosconMonitor(
            energy_v=fields["VEnergy"],
            filament_a=fields["IFilament"],
            emission_a=fields["IEmission"],
            raw=raw,
        )

    @staticmethod
    def _first_numeric_field(fields: dict[str, float], *aliases: str) -> Optional[float]:
        # Firmware revisions have used slightly different labels. Match exact
        # aliases first, then compare normalized names without punctuation/case.
        for alias in aliases:
            if alias in fields:
                return fields[alias]
        normalized = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in fields.items()}
        for alias in aliases:
            key = re.sub(r"[^a-z0-9]", "", alias.lower())
            if key in normalized:
                return normalized[key]
        return None

    def diagnostics(self) -> CosconDiagnostics:
        # Diagnostics are non-critical display values. Use a short timeout so
        # an unavailable command cannot delay the primary safety polling.
        raw = self.send("GetDiagnosticValues", timeout_s=min(0.5, self.timeout_s))
        fields = {key: float(value) for key, value in self.NUMBER_RE.findall(raw)}
        return CosconDiagnostics(
            energy_current_a=self._first_numeric_field(
                fields, "IEnergy", "EnergyCurrent", "IEnergyCurrent", "IBeam"
            ),
            anode_voltage_v=self._first_numeric_field(
                fields, "VAnode", "AnodeVoltage", "VAnodeVoltage"
            ),
            repeller_voltage_v=self._first_numeric_field(
                fields, "VRepeller", "RepellerVoltage", "VRepellerVoltage"
            ),
            raw=raw,
        )

    def validate(self, emission_a: float, energy_v: float) -> None:
        reply = self.send(
            f"ValidateOperateTarget Emission={emission_a:.6e} "
            f"Energy={energy_v:.6g}"
        )
        if "OK" not in reply.upper():
            raise RuntimeError(f"Unexpected validation reply: {reply}")

    def operate(self, emission_a: float, energy_v: float) -> None:
        reply = self.send(
            f"SwitchToOperate Emission={emission_a:.6e} "
            f"Energy={energy_v:.6g}"
        )
        if "OK" not in reply.upper():
            raise RuntimeError(f"Unexpected operate reply: {reply}")

    def activate(self, emission_a: float, energy_v: float, *, quiet_s: float) -> None:
        """Validate and request Operate as one exclusive controller transaction.

        COSCON activation is stateful: after ``ValidateOperateTarget`` the
        firmware needs a short quiet interval before ``SwitchToOperate``.  The
        outer lock is essential because ``send()`` otherwise releases the lock
        between the two datagrams, allowing a background status/diagnostic poll
        to arrive in the middle of the handoff.
        """
        with self.lock:
            self.validate(emission_a, energy_v)
            time.sleep(max(0.0, float(quiet_s)))
            self.operate(emission_a, energy_v)

    def uptime_s(self) -> int:
        raw = self.send("Uptime")
        match = re.search(r"\bUptime\s+OK:\s*(\d+)\b", raw, re.I)
        if not match:
            raise RuntimeError(f"Could not parse COSCON uptime: {raw}")
        return int(match.group(1))


# =============================================================================
# PHASE 02 DASHBOARD
# =============================================================================

HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Automated Sputtering-Annealing</title>
  <style>
    :root {
      --bg:#eef2f7;
      --surface:#ffffff;
      --surface-soft:#f8fafc;
      --border:#dbe3ec;
      --text:#0f172a;
      --muted:#64748b;
      --accent:#4f82df;
      --accent-dark:#2f65bd;
      --accent-soft:#eaf2ff;
      --good:#238b5c;
      --good-soft:#e9f7f0;
      --warn:#bd6b18;
      --warn-soft:#fff4e5;
      --danger:#c84a56;
      --danger-soft:#fff0f1;
      --purple:#7157b9;
      --shadow:0 10px 28px rgba(33,54,77,.09);
    }

    * { box-sizing:border-box; }
    html, body {
      margin:0;
      width:100%;
      height:100%;
      overflow:hidden;
      font-family:Segoe UI, Inter, Arial, sans-serif;
      color:var(--text);
      background:var(--bg);
    }

    body.theme-degassing { --accent:#7d64c7; --accent-dark:#6046aa; --accent-soft:#f0ecfb; }
    body.theme-sputter   { --accent:#e3932e; --accent-dark:#bd6b18; --accent-soft:#fff2df; }
    body.theme-anneal    { --accent:#cf658a; --accent-dark:#a94769; --accent-soft:#fff0f5; }
    body.theme-done      { --accent:#2d9b68; --accent-dark:#21784f; --accent-soft:#eaf8f1; }
    body.theme-abort     { --accent:#d65b65; --accent-dark:#b23f49; --accent-soft:#fff0f1; }

    button, input { font:inherit; }
    .shell {
      height:100vh;
      min-height:0;
      display:grid;
      grid-template-rows:auto minmax(0,1fr);
      gap:12px;
      padding:12px;
    }

    .card {
      background:var(--surface);
      border:1px solid var(--border);
      border-radius:18px;
      box-shadow:var(--shadow);
      min-width:0;
    }

    .header {
      display:grid;
      grid-template-columns:minmax(330px,.8fr) minmax(640px,1.35fr);
      gap:18px;
      align-items:center;
      padding:14px 18px;
      border-top:5px solid var(--accent);
    }
    .eyebrow {
      color:var(--accent-dark);
      font-size:12px;
      font-weight:900;
      letter-spacing:1.2px;
      text-transform:uppercase;
    }
    .title { margin-top:2px; font-size:27px; line-height:1.08; font-weight:950; }
    .subtitle { margin-top:5px; color:var(--muted); font-size:11.5px; line-height:1.35; }

    .runSummary {
      display:grid;
      grid-template-columns:1.15fr .65fr 1fr 1fr;
      gap:8px;
    }
    .summaryItem {
      padding:8px 10px;
      border-radius:11px;
      border:1px solid #e1e8f0;
      background:var(--surface-soft);
      min-width:0;
    }
    .summaryItem span {
      display:block;
      color:var(--muted);
      font-size:8.8px;
      font-weight:900;
      letter-spacing:.68px;
      text-transform:uppercase;
      margin-bottom:2px;
    }
    .summaryItem strong {
      display:block;
      font-size:16px;
      font-weight:950;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }
    .progressWrap { margin-top:8px; }
    .progressTrack { height:7px; overflow:hidden; border-radius:999px; background:#e5ecf4; }
    .progressFill { height:100%; width:0; border-radius:999px; background:linear-gradient(90deg,var(--accent),var(--accent-dark)); transition:width .3s ease; }
    .progressMeta { display:flex; justify-content:space-between; gap:12px; margin-top:4px; color:var(--muted); font-size:9.5px; }

    .main {
      min-height:0;
      display:grid;
      grid-template-columns:minmax(570px,1.28fr) minmax(450px,.92fr);
      gap:12px;
      overflow:hidden;
    }
    .column { min-height:0; display:flex; flex-direction:column; gap:12px; overflow:hidden; }
    .section { padding:14px 16px; }
    .sectionHeader { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:10px; }
    .sectionTitle { font-size:13px; font-weight:950; letter-spacing:.9px; text-transform:uppercase; color:#40556c; }
    .pill { display:inline-flex; align-items:center; padding:4px 8px; border:1px solid #d7e1eb; border-radius:999px; background:#f7f9fc; color:#5f7389; font-size:9.5px; font-weight:850; white-space:nowrap; }

    /* Primary decision area: the only information that demands action now. */
    .taskCard {
      flex:1 1 auto;
      min-height:0;
      display:flex;
      flex-direction:column;
      border-top:5px solid var(--accent);
      background:linear-gradient(145deg,var(--accent-soft),#fff 47%);
    }
    .taskHeader { display:flex; align-items:center; justify-content:space-between; gap:10px; }
    .stageBadge { display:inline-flex; padding:5px 9px; border-radius:999px; background:linear-gradient(180deg,var(--accent),var(--accent-dark)); color:#fff; font-size:10px; font-weight:950; letter-spacing:.65px; text-transform:uppercase; }
    .stageHero { padding:16px; border:1px solid color-mix(in srgb,var(--accent) 28%,white); border-radius:14px; background:rgba(255,255,255,.76); }
    .stageText { font-size:27px; line-height:1.12; font-weight:950; }
    .stageDetails { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.38; white-space:pre-line; }
    .promptLabel { margin:14px 0 5px; color:var(--accent-dark); font-size:10px; font-weight:950; letter-spacing:.75px; text-transform:uppercase; }
    .prompt {
      flex:0 0 auto;
      min-height:92px;
      padding:15px;
      border:1px solid #d5e0eb;
      border-radius:14px;
      background:#fff;
      font-size:15px;
      font-weight:700;
      line-height:1.43;
      white-space:pre-line;
    }
    .actions { display:flex; flex-wrap:wrap; gap:9px; margin-top:10px; }
    .noAction { flex:1; padding:12px; border:1px dashed #c5d2e0; border-radius:11px; background:rgba(255,255,255,.58); color:var(--muted); text-align:center; font-size:11px; }
    button { min-width:148px; padding:11px 14px; border:0; border-radius:11px; color:#fff; font-size:13px; font-weight:900; cursor:pointer; background:linear-gradient(180deg,#71849a,#566a80); box-shadow:0 5px 13px rgba(34,55,77,.16); }
    button:hover { filter:brightness(1.04); }
    button:disabled { opacity:.58; cursor:default; }
    button.primary { background:linear-gradient(180deg,var(--accent),var(--accent-dark)); }
    button.success { background:linear-gradient(180deg,#36af79,#238b5c); }
    button.warning { background:linear-gradient(180deg,#eda847,#c67820); }
    button.abort { min-width:0; width:100%; margin-top:auto; background:linear-gradient(180deg,#df6972,#bc414c); }
    .safetyNote { margin:10px 0 8px; color:var(--muted); font-size:9.8px; line-height:1.3; text-align:center; }

    .timerCard { flex:0 0 auto; border-top:4px solid #2f9fb3; background:#eef9fb; }
    .timers { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
    .timer { padding:9px 10px; border:1px solid #d3e5e9; border-radius:11px; background:rgba(255,255,255,.76); }
    .timer span { display:block; color:var(--muted); font-size:8.5px; font-weight:900; letter-spacing:.55px; text-transform:uppercase; margin-bottom:3px; }
    .timer strong { font-size:19px; font-weight:950; }
    .timerHint { margin-top:7px; color:var(--muted); font-size:9.5px; line-height:1.3; }

    /* Right column: only values needed to judge safety and progress. */
    .criticalCard { flex:0 0 auto; border-top:5px solid #e3932e; background:#fff9f1; }
    .criticalGrid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
    .metric { min-width:0; padding:10px 11px; border:1px solid #dde5ee; border-radius:12px; background:rgba(255,255,255,.82); }
    .metric.wide { grid-column:1 / -1; }
    .metricLabel { color:var(--muted); font-size:8.8px; font-weight:950; letter-spacing:.65px; text-transform:uppercase; margin-bottom:3px; }
    .metricValue { font-size:19px; line-height:1.1; font-weight:950; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .metricSub { margin-top:4px; color:var(--muted); font-size:9.6px; line-height:1.27; }
    .modeRow { display:grid; grid-template-columns:1fr auto; gap:10px; align-items:start; }
    .ok { color:var(--good); }
    .bad { color:var(--danger); }
    .warningText { color:var(--warn); }

    .ovenCard { flex:0 0 auto; border-top:4px solid #cf658a; background:#fff3f7; }
    .ovenGrid { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; }

    .detailsStack { flex:1 1 auto; min-height:0; overflow:auto; display:flex; flex-direction:column; gap:8px; padding-right:2px; }
    details.card { box-shadow:none; border-radius:14px; background:#fff; }
    details > summary { list-style:none; cursor:pointer; display:flex; justify-content:space-between; align-items:center; gap:10px; padding:11px 13px; color:#40566d; font-size:11px; font-weight:950; letter-spacing:.65px; text-transform:uppercase; }
    details > summary::-webkit-details-marker { display:none; }
    details > summary::after { content:'＋'; color:var(--accent-dark); font-size:16px; }
    details[open] > summary::after { content:'−'; }
    .detailsBody { border-top:1px solid #e4eaf1; padding:11px 13px; }

    .workflow { display:flex; flex-direction:column; gap:5px; }
    .step { display:grid; grid-template-columns:27px 1fr auto; gap:8px; align-items:center; padding:7px 8px; border:1px solid #e2e8ef; border-radius:9px; background:#fafbfd; }
    .stepNumber { width:27px; height:27px; display:flex; align-items:center; justify-content:center; border-radius:50%; background:#e8eef5; color:#6c8095; font-size:10px; font-weight:950; }
    .step.done { background:var(--good-soft); border-color:#c9e6d7; }
    .step.done .stepNumber { color:#fff; background:var(--good); }
    .step.active { background:var(--accent-soft); border-color:color-mix(in srgb,var(--accent) 45%,white); }
    .step.active .stepNumber { color:#fff; background:linear-gradient(180deg,var(--accent),var(--accent-dark)); }
    .step.skipped { opacity:.48; }
    .stepName { font-size:11.3px; font-weight:950; }
    .stepDesc { margin-top:1px; color:var(--muted); font-size:8.9px; line-height:1.23; }
    .stepTime { max-width:92px; color:#60748a; font-size:8.7px; font-weight:900; text-align:right; }

    .pidBox { padding:10px; border:1px solid #cfdcf0; border-radius:11px; background:#f7faff; font-size:10px; line-height:1.35; }
    .pidRow { display:grid; grid-template-columns:1fr auto; gap:7px; margin-top:7px; }
    input { min-width:0; padding:9px; border:1px solid #c9d5e2; border-radius:9px; color:var(--text); background:#fff; font-weight:850; outline:none; }
    .pidStatus { margin-top:6px; color:#486784; font-size:9px; }

    .statusGrid { display:grid; grid-template-columns:repeat(2,1fr); gap:7px; }
    .errorBox { grid-column:1 / -1; min-height:48px; padding:10px; border:1px solid #cfe4d8; border-radius:10px; color:#287553; background:#f0faf5; font-size:10px; line-height:1.35; white-space:pre-line; }
    .errorBox.active { border-color:#edb7bc; color:#a53640; background:var(--danger-soft); }
    .reminder { color:#586d82; font-size:9.7px; line-height:1.4; }
    .interlockHelp { margin-top:8px; padding:8px 9px; border-left:3px solid #e3932e; background:#fff8ee; color:#6a5844; font-size:9.2px; line-height:1.35; }

    @media (max-width:1120px), (max-height:720px) {
      html, body { height:auto; min-height:100%; overflow:auto; }
      .shell { height:auto; min-height:100vh; }
      .header { grid-template-columns:1fr; }
      .main { grid-template-columns:1fr; overflow:visible; }
      .column, .detailsStack { overflow:visible; }
      .taskCard { min-height:500px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="card header">
      <div>
        <div class="eyebrow">NPG chamber · Phase 02</div>
        <div class="title">Automated Sputtering-Annealing</div>
        <div class="subtitle">Task-focused operator view. COSCON control and verification are automatic; the argon leak valve remains manual.</div>
      </div>
      <div>
        <div class="runSummary">
          <div class="summaryItem"><span>Run</span><strong id="runName">--</strong></div>
          <div class="summaryItem"><span>Cycle</span><strong id="cycleText">0 / 0</strong></div>
          <div class="summaryItem"><span>Stage</span><strong id="headerStage">INIT</strong></div>
          <div class="summaryItem"><span>System</span><strong id="headerHealth">Waiting</strong></div>
        </div>
        <div class="progressWrap">
          <div class="progressTrack"><div id="progressFill" class="progressFill"></div></div>
          <div class="progressMeta"><span id="progressText">Workflow progress: 0%</span><span>Supervision required</span></div>
        </div>
      </div>
    </header>

    <main class="main">
      <section class="column">
        <div class="card section taskCard">
          <div class="taskHeader">
            <div class="sectionTitle">What to do now</div>
            <div id="stageBadge" class="stageBadge">INIT</div>
          </div>
          <div class="stageHero">
            <div id="stageText" class="stageText">Preparing Phase 02</div>
            <div id="stageDetails" class="stageDetails">Waiting for telemetry and instructions.</div>
          </div>
          <div class="promptLabel">Operator instruction</div>
          <div id="prompt" class="prompt">Waiting for instructions…</div>
          <div id="actionButtons" class="actions"></div>
          <div class="safetyNote">The safe-stop button is always available. It does not replace checking the physical chamber and local instrument controls.</div>
          <button id="abortButton" class="abort" type="button">Abort / Safe Stop</button>
        </div>

        <div class="card section timerCard">
          <div class="sectionHeader"><div class="sectionTitle">Timing</div><div class="pill">Live</div></div>
          <div class="timers">
            <div class="timer"><span id="currentTimerLabel">Current step</span><strong id="currentTimer">--:--</strong></div>
            <div class="timer"><span>Stage elapsed</span><strong id="stageElapsed">00:00</strong></div>
            <div class="timer"><span>Run elapsed</span><strong id="runElapsed">00:00</strong></div>
            <div class="timer"><span>Known timed work left</span><strong id="knownRemaining">--:--</strong></div>
          </div>
          <div class="timerHint">Degas and oven ramp duration depend on the hardware. During Degas, the countdown is the safety timeout, not a predicted finish time.</div>
        </div>
      </section>

      <section class="column">
        <div class="card section criticalCard">
          <div class="sectionHeader"><div class="sectionTitle">Critical process values</div><div id="cosconHealth" class="pill">Waiting</div></div>
          <div class="criticalGrid">
            <div class="metric"><div class="metricLabel">Chamber pressure</div><div id="pressureVal" class="metricValue">--</div><div id="pressureSub" class="metricSub">Target near 2×10⁻⁵ mbar</div></div>
            <div class="metric"><div class="metricLabel">Energy</div><div id="energyVal" class="metricValue">-- V</div><div id="energySub" class="metricSub">Target: 2250 V</div></div>
            <div class="metric"><div class="metricLabel">Emission</div><div id="emissionVal" class="metricValue">-- mA</div><div id="emissionSub" class="metricSub">Target: 10 mA</div></div>
          </div>
        </div>

        <div class="card section ovenCard">
          <div class="sectionHeader"><div class="sectionTitle">Oven</div><div class="pill">PV / SV</div></div>
          <div class="ovenGrid">
            <div class="metric"><div class="metricLabel">Oven PV</div><div id="pvVal" class="metricValue">-- °C</div></div>
            <div class="metric"><div class="metricLabel">Oven SV</div><div id="svVal" class="metricValue">-- °C</div></div>
          </div>
          <div id="pidControlCard" style="display:none; margin-top:9px">
            <div class="pidBox">
              <strong>Live PID SV control</strong><br>Available only during the oven ramp or anneal hold.
              <div class="pidRow"><input id="pidSvInput" type="number" step="1" inputmode="decimal" placeholder="New SV, e.g. 620"><button id="setSvBtn" class="primary" type="button">Set PID SV</button></div>
              <div id="pidCommandStatus" class="pidStatus">No PID command pending.</div>
            </div>
          </div>
        </div>

        <div class="detailsStack">
          <details class="card">
            <summary>Current-cycle workflow</summary>
            <div class="detailsBody"><div id="workflow" class="workflow"></div></div>
          </details>

          <details class="card">
            <summary>Auxiliary diagnostics</summary>
            <div class="detailsBody">
              <div class="statusGrid">
                <div class="metric"><div class="metricLabel">COSCON mode</div><div id="cosconMode" class="metricValue">--</div><div id="cosconDetails" class="metricSub">No status details yet</div></div>
                <div class="metric"><div class="metricLabel">Safety permission</div><div id="interlockVal" class="metricValue">--</div><div class="metricSub">Internal hardware interlock</div></div>
                <div class="metric"><div class="metricLabel">Last telemetry</div><div id="updatedAt" class="metricValue">--</div></div>
                <div class="metric"><div class="metricLabel">Filament current</div><div id="filamentVal" class="metricValue">-- A</div></div>
                <div class="metric"><div class="metricLabel">Energy current</div><div id="energyCurrentVal" class="metricValue">-- A</div></div>
                <div class="metric"><div class="metricLabel">Anode voltage</div><div id="anodeVoltageVal" class="metricValue">-- V</div></div>
                <div class="metric"><div class="metricLabel">Repeller voltage</div><div id="repellerVoltageVal" class="metricValue">-- V</div></div>
                <div class="metric"><div class="metricLabel">Keysight voltage</div><div id="keysightV" class="metricValue">-- V</div></div>
                <div class="metric"><div class="metricLabel">Keysight current</div><div id="keysightI" class="metricValue">-- A</div></div>
                <div id="errorBox" class="errorBox">No active errors.</div>
              </div>
            </div>
          </details>

          <details class="card">
            <summary>Safety reminders</summary>
            <div class="detailsBody">
              <div class="reminder">
                Keep the COSCON webpage and SpecsLab/Prodigy closed while Phase 02 is active.<br><br>
                Degas is automated unless <b>Start without initial Degas</b> was selected in the launcher. Target validation, Operate, output qualification, sputtering timing and return to Standby are automated. The leak valve remains manual.<br><br>
                Only skip Degas when continuing the same chamber preparation after an earlier partial Phase 02 run and the operator has verified that another Degas is not required.<br><br>
                Supervise the first complete three-cycle runs and keep the hardware emergency stop accessible.
              </div>
            </div>
          </details>
        </div>
      </section>
    </main>

<script>
  const ACTIONS = {
    yes: {label:'Yes', cls:'success'},
    no:  {label:'No', cls:''},
    r:   {label:'Start run', cls:'primary'},
    o:   {label:'Valve opened', cls:'warning'},
    c:   {label:'Valve closed', cls:'warning'}
  };

  const WORKFLOW_STEPS = [
    {key:'DEGASSING', name:'COSCON Degas', desc:'Automatic in cycle 1; completion detected from Standby', time:'device controlled'},
    {key:'OPEN_VALVE', name:'Open argon valve', desc:'Operator confirmation', time:'manual'},
    {key:'PRESSURE_CONDITIONING', name:'Pressure stabilization', desc:'COSCON remains safely inactive', time:'60 s'},
    {key:'COSCON_ACTIVATION', name:'COSCON activation', desc:'Validate + Operate + output verification', time:'~10–35 s'},
    {key:'SPUTTERING', name:'Sputtering', desc:'Continuous output and pressure checks', time:'recipe timer'},
    {key:'COSCON_STANDBY', name:'Return to Standby', desc:'Automatic safe-state verification', time:'≤25 s'},
    {key:'CLOSE_VALVE', name:'Close argon valve', desc:'After automatic safe-state return', time:'manual'},
    {key:'ANNEAL_RAMP', name:'Oven ramp', desc:'Wait for target temperature', time:'variable'},
    {key:'ANNEAL_HOLD', name:'Anneal hold', desc:'Timed hold at target', time:'recipe timer'},
    {key:'ANNEAL_RESET', name:'PID reset', desc:'Automatic cycle handoff', time:'automatic'}
  ];

  const STAGE_ORDER = {
    INIT:-2, PREFLIGHT:-1, DEGASSING:0, OPEN_VALVE:1,
    PRESSURE_CONDITIONING:2, COSCON_ACTIVATION:3,
    SPUTTERING:4, COSCON_STANDBY:5, CLOSE_VALVE:6, ANNEAL_RAMP:7,
    ANNEAL_HOLD:8, ANNEAL_RESET:9, DONE:10, ABORTED:10
  };

  const el = id => document.getElementById(id);
  const promptEl = el('prompt');
  const actionButtons = el('actionButtons');
  const abortButton = el('abortButton');
  const setSvBtn = el('setSvBtn');
  const pidSvInput = el('pidSvInput');
  const pidCommandStatus = el('pidCommandStatus');

  let latestSnapshot = {};

  function fmtNumber(value, digits=2, fallback='--') {
    const n = Number(value);
    if (value === null || value === undefined || value === '' || Number.isNaN(n)) return fallback;
    return n.toFixed(digits);
  }

  function formatDuration(seconds) {
    const n = Number(seconds);
    if (seconds === null || seconds === undefined || seconds === '' || Number.isNaN(n)) return '--:--';
    const s = Math.max(0, Math.round(n));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h > 0
      ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`
      : `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
  }

  function themeFor(stage) {
    const s = String(stage || '').toUpperCase();
    if (s.includes('ABORT')) return 'theme-abort';
    if (s.includes('DONE')) return 'theme-done';
    if (s.includes('DEGASS')) return 'theme-degassing';
    if (s.includes('SPUTTER') || s.includes('COSCON_ACTIVATION')) return 'theme-sputter';
    if (s.includes('ANNEAL')) return 'theme-anneal';
    return '';
  }

  async function sendToken(token) {
    try {
      await window.pywebview.api.send_token(token);
    } catch (err) {
      el('errorBox').classList.add('active');
      el('errorBox').textContent = `Could not send GUI action: ${err}`;
    }
  }

  function renderActions(tokens) {
    actionButtons.innerHTML = '';
    const allowed = (tokens || []).filter(t => t !== 'abort');
    if (!allowed.length) {
      const box = document.createElement('div');
      box.className = 'noAction';
      box.textContent = 'No operator button is required at this moment. Automation is running.';
      actionButtons.appendChild(box);
      return;
    }

    allowed.forEach(token => {
      const cfg = ACTIONS[token] || {label:token, cls:''};
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = cfg.label;
      btn.className = cfg.cls;
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        await sendToken(token);
        promptEl.textContent = 'Action received. Waiting for the next step…';
        renderActions([]);
      });
      actionButtons.appendChild(btn);
    });
  }

  function renderPrompt(payload) {
    promptEl.textContent = payload.message || 'Waiting for instructions…';
    renderActions(payload.allowed || []);
  }

  function renderWorkflow(snap) {
    const stage = String(snap.stage || 'INIT').toUpperCase();
    const currentIndex = STAGE_ORDER[stage] ?? -2;
    const cycle = Number(snap.cycle || 0);
    const container = el('workflow');
    container.innerHTML = '';

    const initialDegasSkipped = Boolean(snap.start_without_degassing);

    WORKFLOW_STEPS.forEach((step, idx) => {
      const row = document.createElement('div');
      row.className = 'step';
      if (idx < currentIndex) row.classList.add('done');
      if (idx === currentIndex) row.classList.add('active');
      if (idx === 0 && (cycle > 1 || initialDegasSkipped)) {
        row.classList.remove('done', 'active');
        row.classList.add('skipped');
      }

      const number = document.createElement('div');
      number.className = 'stepNumber';
      number.textContent = (idx === 0 && (cycle > 1 || initialDegasSkipped)) ? '—' : String(idx + 1);

      const text = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'stepName';
      name.textContent = step.name;
      const desc = document.createElement('div');
      desc.className = 'stepDesc';
      if (idx === 0 && initialDegasSkipped) {
        desc.textContent = 'Skipped by launcher setting for this continuation run';
      } else if (idx === 0 && cycle > 1) {
        desc.textContent = 'Skipped after cycle 1';
      } else {
        desc.textContent = step.desc;
      }
      text.append(name, desc);

      const timing = document.createElement('div');
      timing.className = 'stepTime';
      let stepTime = step.time;
      if (step.key === 'PRESSURE_CONDITIONING' && snap.pressure_conditioning_total_s) {
        stepTime = formatDuration(snap.pressure_conditioning_total_s);
      }
      if (step.key === 'COSCON_ACTIVATION' && snap.operate_timeout_s) {
        stepTime = `≤${formatDuration(snap.operate_timeout_s)}`;
      }
      if (step.key === 'SPUTTERING' && snap.sputter_total_s) {
        stepTime = formatDuration(snap.sputter_total_s);
      }
      if (step.key === 'ANNEAL_HOLD' && snap.anneal_hold_total_s) {
        stepTime = formatDuration(snap.anneal_hold_total_s);
      }
      timing.textContent = stepTime;
      row.append(number, text, timing);
      container.appendChild(row);
    });
  }

  function setHealth(snap) {
    const interlock = String(snap.coscon_interlock || '--');
    const mode = String(snap.coscon_mode || '--');
    const interlockLower = interlock.toLowerCase();
    const modeLower = mode.toLowerCase();
    const waiting = ['--', ''].includes(interlockLower) || ['--', ''].includes(modeLower);
    const healthy = !waiting && interlockLower === 'ok' && modeLower !== 'error';
    const label = waiting ? 'Waiting' : (healthy ? 'Ready' : 'Check');
    const stateClass = waiting ? '' : (healthy ? 'ok' : 'bad');
    const health = el('cosconHealth');
    health.textContent = label;
    health.className = `pill ${stateClass}`;
    el('headerHealth').textContent = label;
    el('headerHealth').className = stateClass;
    el('interlockVal').textContent = interlock;
    el('interlockVal').className = `metricValue ${waiting ? '' : (interlockLower === 'ok' ? 'ok' : 'bad')}`;
  }

  function renderSnapshot(snap) {
    latestSnapshot = snap || {};
    const stage = String(snap.stage || 'INIT');
    document.body.className = themeFor(stage);

    el('runName').textContent = snap.run_name || '--';
    el('cycleText').textContent = `${snap.cycle || 0} / ${snap.total_cycles || 0}`;
    el('headerStage').textContent = stage;
    el('stageBadge').textContent = stage;
    el('stageText').textContent = snap.stage_title || stage.replaceAll('_', ' ');
    el('stageDetails').textContent = snap.stage_description || 'Automation status is being updated.';

    const progress = Math.max(0, Math.min(100, Number(snap.overall_progress_percent || 0)));
    el('progressFill').style.width = `${progress}%`;
    el('progressText').textContent = `Workflow progress: ${progress.toFixed(0)}%`;

    el('currentTimerLabel').textContent = snap.phase_timer_label || 'Current step';
    el('currentTimer').textContent = formatDuration(snap.phase_remaining_s);
    el('stageElapsed').textContent = formatDuration(snap.stage_elapsed_s);
    el('runElapsed').textContent = formatDuration(snap.run_elapsed_s);
    el('knownRemaining').textContent = formatDuration(snap.estimated_timed_remaining_s);

    el('cosconMode').textContent = snap.coscon_mode || '--';
    el('cosconDetails').textContent = snap.coscon_details || 'No status details yet';
    el('energyVal').textContent = `${fmtNumber(snap.coscon_energy_v, 2)} V`;
    el('energySub').textContent = `Target: ${fmtNumber(snap.coscon_target_energy_v, 0)} V`;
    el('emissionVal').textContent = `${fmtNumber(Number(snap.coscon_emission_a) * 1000, 3)} mA`;
    const badEmissionReads = Number(snap.coscon_emission_bad_samples || 0);
    const emissionToleranceMa = Number(snap.coscon_emission_tolerance_a || 0) * 1000;
    const requiredBadReads = Number(snap.coscon_emission_fault_samples || 3);
    el('emissionVal').className = `metricValue ${badEmissionReads > 0 ? 'bad' : ''}`;
    el('emissionSub').textContent = badEmissionReads > 0
      ? `Outside ±${fmtNumber(emissionToleranceMa, 3)} mA: recheck ${badEmissionReads}/${requiredBadReads}`
      : `Target: ${fmtNumber(Number(snap.coscon_target_emission_a) * 1000, 3)} mA`;
    el('filamentVal').textContent = `${fmtNumber(snap.coscon_filament_a, 3)} A`;
    el('energyCurrentVal').textContent = `${fmtNumber(snap.coscon_energy_current_a, 4)} A`;
    el('anodeVoltageVal').textContent = `${fmtNumber(snap.coscon_anode_voltage_v, 2)} V`;
    el('repellerVoltageVal').textContent = `${fmtNumber(snap.coscon_repeller_voltage_v, 2)} V`;

    const pressure = Number(snap.pressure_mbar);
    el('pressureVal').textContent = Number.isNaN(pressure) ? '--' : pressure.toExponential(3);
    const pressureHigh = !Number.isNaN(pressure) && pressure > Number(snap.pressure_warning_mbar || 3e-5);
    el('pressureVal').className = `metricValue ${pressureHigh ? 'bad' : ''}`;
    el('pressureSub').textContent = pressureHigh
      ? `Above warning limit ${Number(snap.pressure_warning_mbar || 3e-5).toExponential(1)} mbar`
      : 'Target near 2×10⁻⁵ mbar';

    el('pvVal').textContent = `${fmtNumber(snap.oven_pv_c, 1)} °C`;
    el('svVal').textContent = `${fmtNumber(snap.oven_sv_c, 1)} °C`;
    el('keysightV').textContent = `${fmtNumber(snap.keysight_voltage_v, 3)} V`;
    el('keysightI').textContent = `${fmtNumber(snap.keysight_current_a, 4)} A`;

    const pidAvailable = ['ANNEAL_RAMP', 'ANNEAL_HOLD'].includes(stage.toUpperCase());
    el('pidControlCard').style.display = pidAvailable ? '' : 'none';
    setSvBtn.disabled = !pidAvailable;

    const error = String(snap.last_error || '').trim();
    const errorBox = el('errorBox');
    errorBox.classList.toggle('active', Boolean(error));
    errorBox.textContent = error ? `Last active error:\n${error}` : 'No active errors.';
    el('updatedAt').textContent = snap.last_update ? `Updated ${snap.last_update}` : 'No telemetry';

    setHealth(snap);
    renderWorkflow(snap);
  }

  abortButton.addEventListener('click', async () => {
    abortButton.disabled = true;
    abortButton.textContent = 'Safe stop requested…';
    await sendToken('abort');
  });

  setSvBtn.addEventListener('click', async () => {
    const stage = String(latestSnapshot.stage || '').toUpperCase();
    if (!['ANNEAL_RAMP', 'ANNEAL_HOLD'].includes(stage)) {
      pidCommandStatus.textContent = 'PID SV changes are available only during the oven ramp or anneal hold.';
      return;
    }
    const raw = String(pidSvInput.value || '').trim().replace(',', '.');
    const value = Number(raw);
    if (!raw || Number.isNaN(value)) {
      pidCommandStatus.textContent = 'Enter a valid numerical PID setpoint.';
      return;
    }
    pidCommandStatus.textContent = `Requesting PID SV = ${value} °C…`;
    await sendToken(`pid_sv:${value}`);
  });

  async function pump() {
    try {
      const messages = await window.pywebview.api.pull_messages();
      for (const msg of messages) {
        if (msg.kind === 'prompt') renderPrompt(msg.payload);
        if (msg.kind === 'snapshot') renderSnapshot(msg.payload);
        if (msg.kind === 'pid_status') pidCommandStatus.textContent = msg.payload;
        if (msg.kind === 'close') window.close();
      }
    } catch (err) {
      const errorBox = el('errorBox');
      errorBox.classList.add('active');
      errorBox.textContent = `GUI communication error: ${err}`;
    }
    setTimeout(pump, 220);
  }

  renderActions([]);
  renderWorkflow({stage:'INIT', cycle:0});
  pump();
</script>
</body>
</html>
"""


class UnifiedUIApi:
    def __init__(self, command_q: mp.Queue, event_q: mp.Queue) -> None:
        self.command_q = command_q
        self.event_q = event_q

    def send_token(self, token: str) -> bool:
        self.event_q.put(("token", token))
        return True

    def pull_messages(self) -> list[dict]:
        out = []
        try:
            while True:
                kind, payload = self.command_q.get_nowait()
                out.append({"kind": kind, "payload": payload})
        except queue.Empty:
            pass
        return out


def _unified_ui_target(
    command_q: mp.Queue,
    event_q: mp.Queue,
    ready_event: mp.Event,
    startup_q: mp.Queue,
    title: str,
    width: int,
    height: int,
) -> None:
    try:
        import webview
        api = UnifiedUIApi(command_q, event_q)
        webview.create_window(
            title,
            html=HTML_TEMPLATE,
            js_api=api,
            width=width,
            height=height,
            confirm_close=True,
        )

        def mark_ready() -> None:
            ready_event.set()

        # The callback runs only after pywebview has successfully selected and
        # initialized its Windows backend. This prevents the parent process from
        # entering PREFLIGHT when pythonnet/WinForms has already crashed.
        webview.start(mark_ready, debug=False)
    except BaseException:
        try:
            startup_q.put(traceback.format_exc())
        except Exception:
            pass
        raise


class UnifiedUIClient:
    def __init__(self, enabled: bool, title: str, width: int, height: int) -> None:
        self.enabled = enabled
        self.title = title
        self.width = width
        self.height = height
        self.process: Optional[mp.Process] = None
        self.command_q: Optional[mp.Queue] = None
        self.event_q: Optional[mp.Queue] = None
        self.ready_event: Optional[mp.Event] = None
        self.startup_q: Optional[mp.Queue] = None
        self.last_startup_error: str = ""
        self.special_token_handler: Optional[Callable[[str], bool]] = None
        # The monitor thread and the controller thread both need to inspect the
        # UI event queue.  Normal operator tokens are retained here when the
        # monitor thread sees them, so a manual action cannot be silently
        # consumed while the controller is waiting for it.
        self._pending_tokens: list[str] = []
        self._pending_tokens_lock = threading.Lock()
        self._abort_requested = threading.Event()
        self._console_input_buffer = ""

    def start(self) -> bool:
        if not self.enabled:
            return False
        try:
            import webview  # noqa: F401
        except Exception:
            return False
        if self.process is not None and self.process.is_alive():
            return True
        self.command_q = mp.Queue()
        self.event_q = mp.Queue()
        self.ready_event = mp.Event()
        self.startup_q = mp.Queue()
        self.last_startup_error = ""
        self._abort_requested.clear()
        with self._pending_tokens_lock:
            self._pending_tokens.clear()
        self._console_input_buffer = ""
        self.process = mp.Process(
            target=_unified_ui_target,
            args=(
                self.command_q,
                self.event_q,
                self.ready_event,
                self.startup_q,
                self.title,
                self.width,
                self.height,
            ),
            daemon=True,
        )
        self.process.start()

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self.ready_event.wait(timeout=0.10):
                return True
            if not self.process.is_alive():
                break

        if self.startup_q is not None:
            try:
                self.last_startup_error = self.startup_q.get_nowait().strip()
            except queue.Empty:
                pass
        self.close()
        return False

    def is_running(self) -> bool:
        return self.process is not None and self.process.is_alive() and self.command_q is not None and self.event_q is not None

    def set_stage(self, text: str) -> None:
        if self.is_running():
            self.command_q.put(("stage", text))

    def set_snapshot(self, snap: dict) -> None:
        if self.is_running():
            self.command_q.put(("snapshot", snap))

    def set_pid_status(self, text: str) -> None:
        if self.is_running():
            self.command_q.put(("pid_status", text))

    def _handle_special_token(self, token: str) -> bool:
        if self.special_token_handler is None:
            return False
        try:
            return bool(self.special_token_handler(token))
        except Exception:
            return False

    def _buffer_token(self, token: str) -> None:
        with self._pending_tokens_lock:
            self._pending_tokens.append(token)

    def _pop_buffered_token(self) -> Optional[str]:
        with self._pending_tokens_lock:
            if not self._pending_tokens:
                return None
            return self._pending_tokens.pop(0)

    def _process_wait_token(self, token: str, allowed: list[str]) -> Optional[str]:
        if token == "abort":
            self._abort_requested.set()
            raise KeyboardInterrupt("Abort requested from unified UI or CMD")
        if self._handle_special_token(token):
            return None
        if token in allowed:
            return token
        return None

    def raise_if_abort_requested(self) -> None:
        if self._abort_requested.is_set():
            raise KeyboardInterrupt("Abort requested from unified UI or CMD")

    def _announce_console_fallback(self, allowed: list[str], message: str, reason: str) -> None:
        allowed_text = ", ".join(repr(token) for token in allowed)
        print(
            f"\nDashboard confirmation fallback ({reason}).\n"
            f"{message}\n"
            f"Type one of {allowed_text} followed by Enter in this CMD, "
            "or type 'abort' to request the safe stop."
        )

    def _read_console_token(self) -> Optional[str]:
        """Read one console line without blocking the hardware-control thread."""

        try:
            if os.name == "nt":
                import msvcrt

                while msvcrt.kbhit():
                    char = msvcrt.getwch()
                    if char in ("\x00", "\xe0"):
                        # Function and arrow keys produce a two-character
                        # sequence.  They are not operator tokens.
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                        continue
                    if char in ("\r", "\n"):
                        print()
                        token = self._console_input_buffer.strip().lower()
                        self._console_input_buffer = ""
                        return token or None
                    if char == "\x03":
                        raise KeyboardInterrupt("Abort requested from CMD")
                    if char == "\b":
                        if self._console_input_buffer:
                            self._console_input_buffer = self._console_input_buffer[:-1]
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                        continue
                    self._console_input_buffer += char
                    sys.stdout.write(char)
                    sys.stdout.flush()
                return None

            import select

            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if not readable:
                return None
            line = sys.stdin.readline()
            if line == "":
                return None
            return line.strip().lower() or None
        except (OSError, TypeError, ValueError):
            # A redirected/closed stdin is not a reason to block forever.  The
            # prompt watchdog below will enter the normal safe-stop path.
            return None

    def poll_background_tokens(self) -> None:
        if not self.is_running():
            return
        while True:
            try:
                kind, token = self.event_q.get_nowait()
            except queue.Empty:
                return
            if kind != "token":
                continue
            if token == "abort":
                self._abort_requested.set()
                continue
            if self._handle_special_token(token):
                continue
            # Do not discard normal action tokens.  This method is also called
            # by the telemetry thread, which must not steal a token from the
            # controller's blocking/manual-action path.
            self._buffer_token(token)

    def wait_for_token(self, allowed: list[str], message: str) -> str:
        prompt_started = time.monotonic()
        dashboard_loss_announced = not self.is_running()
        if self.is_running():
            self.command_q.put(("prompt", {"allowed": allowed, "message": message}))
            self._announce_console_fallback(
                allowed,
                message,
                "CMD fallback available if the dashboard is unresponsive",
            )
        else:
            self._announce_console_fallback(allowed, message, "dashboard unavailable")

        while True:
            self.raise_if_abort_requested()

            buffered = self._pop_buffered_token()
            if buffered is not None:
                accepted = self._process_wait_token(buffered, allowed)
                if accepted is not None:
                    return accepted
                continue

            if self.is_running():
                try:
                    kind, token = self.event_q.get(timeout=UI_TOKEN_POLL_S)
                except queue.Empty:
                    pass
                else:
                    if kind == "token":
                        accepted = self._process_wait_token(token, allowed)
                        if accepted is not None:
                            return accepted
                        continue
            elif not dashboard_loss_announced:
                self._announce_console_fallback(
                    allowed,
                    message,
                    "dashboard stopped while waiting",
                )
                dashboard_loss_announced = True

            console_token = self._read_console_token()
            if console_token is not None:
                accepted = self._process_wait_token(console_token, allowed)
                if accepted is not None:
                    return accepted

            elapsed = time.monotonic() - prompt_started
            if elapsed >= OPERATOR_PROMPT_TIMEOUT_S:
                raise RuntimeError(
                    "Operator confirmation timed out after "
                    f"{OPERATOR_PROMPT_TIMEOUT_S / 60:.0f} minutes; "
                    "the Phase 02 safe-stop path will now run."
                )

            # A dead dashboard has no queue wait to pace this loop.
            if not self.is_running():
                time.sleep(UI_TOKEN_POLL_S)

    def close(self) -> None:
        if self.is_running():
            self.command_q.put(("close", None))
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2)
        with self._pending_tokens_lock:
            self._pending_tokens.clear()


# =============================================================================
# CONTROLLER
# =============================================================================

class SputterAnnealController:
    def __init__(self, config: RunConfig) -> None:
        self.cfg = config
        self.state = MonitorState()
        self.stop_event = mp.Event()
        self.aborted = False
        self.run_started_at = time.time()
        self.stage_started_at = self.run_started_at
        # Phase 02 intentionally raises chamber pressure to ~2e-5 mbar with Ar.
        # Therefore its desktop popup follows the existing 1e-4 mbar emergency
        # threshold, not the 5e-6 UHV warning used in Phases 01 and 03.
        self.pressure_emergency_alarm = PressureEmergencyAlarm(
            threshold_mbar=self.cfg.pressure_emergency_mbar,
            context='Phase 02 - Sputtering-Annealing',
        )

        base_dir = make_short_output_dir(self.cfg.run_name)
        self.logger = DataLogger(base_dir)
        self.output_dir = base_dir
        try:
            write_effective_parameters(
                os.path.join(self.output_dir, "automation_parameters.json"),
                "sputter",
                RUN_AUTOMATION_OVERRIDES,
            )
        except Exception as exc:
            warn(f"Could not save effective automation parameters: {exc}")

        self.xgs600 = XGS600Gauge(self.cfg.xgs600_port, self.cfg.xgs600_baud)
        self.pid = OvenPID(self.cfg.pid_port, self.cfg.pid_baud, self.cfg.pid_address)
        self.keysight = KeysightSupply(self.cfg.keysight_port, self.cfg.keysight_baud) if self.cfg.keysight_port else None
        self.coscon = CosconUDP(self.cfg.coscon_ip, self.cfg.coscon_udp_port, self.cfg.coscon_udp_timeout_s)
        self.coscon_activation_requested = False
        # The normal telemetry worker must not interleave COSCON datagrams with
        # the documented Reset/reboot handshake. Other chamber telemetry keeps
        # running while this event is set.
        self._coscon_polling_paused = threading.Event()

        self.ui = UnifiedUIClient(
            enabled=True,
            title=self.cfg.coscon_window_title,
            width=self.cfg.coscon_window_width,
            height=self.cfg.coscon_window_height,
        )
        self.ui.special_token_handler = self._handle_special_ui_token
        self.monitor_thread = mp.Process  # placeholder to satisfy type checkers
        self._monitor_thread = None

    def _format_remaining(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "--:--"
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _set_phase_timer(
        self,
        remaining_s: Optional[int] = None,
        total_s: Optional[int] = None,
        label: str = "Remaining",
    ) -> None:
        self.state.phase_remaining_s = None if remaining_s is None else max(0, int(remaining_s))
        self.state.phase_total_s = None if total_s is None else max(0, int(total_s))
        self.state.phase_timer_label = label
        self._update_ui_status()

    def _clear_phase_timer(self, label: str = "Remaining") -> None:
        self.state.phase_remaining_s = None
        self.state.phase_total_s = None
        self.state.phase_timer_label = label
        self._update_ui_status()

    def _run_phase_countdown_until_stop(
        self,
        total_s: int,
        label: str,
        stop_event: threading.Event,
    ) -> None:
        start = time.time()
        self._set_phase_timer(total_s, total_s, label)
        while not stop_event.is_set():
            elapsed = int(time.time() - start)
            remaining = total_s - elapsed
            self._set_phase_timer(remaining, total_s, label)
            if remaining <= 0:
                break
            stop_event.wait(1.0)

    def _set_stage(self, stage: str, cycle: Optional[int] = None) -> None:
        self.state.stage = stage
        self.stage_started_at = time.time()
        self.state.phase_remaining_s = None
        self.state.phase_total_s = None
        self.state.phase_timer_label = "Current step"
        if cycle is not None:
            self.state.cycle = cycle
        self._update_ui_status()

    def _stage_title_description(self, stage: str) -> tuple[str, str]:
        labels = {
            "INIT": ("Preparing Phase 02", "Opening devices and starting the control dashboard."),
            "PREFLIGHT": ("Preflight checks", "Confirm the physical sputter-gun and electronics preparation."),
            "DEGASSING": ("Automatic COSCON Degas", "Cycle 1 only. The script waits for natural Standby completion."),
            "OPEN_VALVE": ("Open the argon leak valve", "Manual action: stabilize pressure near 2×10⁻⁵ mbar."),
            "PRESSURE_CONDITIONING": ("Pressure conditioning", "COSCON remains safely inactive while pressure stability is verified."),
            "COSCON_ACTIVATION": ("COSCON activation", "Validate target, enter Operating and qualify measured energy/emission."),
            "SPUTTERING": ("Sputtering in progress", "Pressure, mode, interlock, energy and emission are checked continuously."),
            "COSCON_STANDBY": ("Returning COSCON to Standby", "The sputtering output is being disabled and the safe state is verified."),
            "CLOSE_VALVE": ("Close the argon leak valve", "COSCON is already in Standby. Close the valve fully."),
            "ANNEAL_RAMP": ("Oven ramp", "The PID target is active. Waiting for the measured temperature window."),
            "ANNEAL_HOLD": ("Annealing hold", "The oven is within the accepted target window and the timed hold is running."),
            "ANNEAL_RESET": ("Cycle reset", "The PID setpoint is being reset before the next cycle."),
            "DONE": ("Phase 02 complete", "All configured sputter-anneal cycles finished normally."),
            "ABORTED": ("Safe stop / aborted", "The automatic shutdown path is active. Check the physical equipment."),
        }
        return labels.get(stage, (stage.replace("_", " ").title(), "Automation status is being updated."))

    def _estimated_timed_remaining_s(self) -> int:
        """Approximate known timed work left.

        Manual valve waits and oven-ramp time are deliberately excluded because
        they cannot be predicted reliably. Degas uses the configured expected
        duration rather than claiming an exact completion time.
        """
        if self.state.stage in {"DONE", "ABORTED"}:
            return 0

        stage_order = [
            "DEGASSING",
            "OPEN_VALVE",
            "PRESSURE_CONDITIONING",
            "COSCON_ACTIVATION",
            "SPUTTERING",
            "COSCON_STANDBY",
            "CLOSE_VALVE",
            "ANNEAL_RAMP",
            "ANNEAL_HOLD",
            "ANNEAL_RESET",
        ]
        timed = {
            "PRESSURE_CONDITIONING": int(self.cfg.standby_conditioning_s),
            "COSCON_ACTIVATION": int(self.cfg.operate_transition_timeout_s),
            "SPUTTERING": int(self.cfg.sputter_minutes * 60),
            "ANNEAL_HOLD": int(self.cfg.anneal_hold_minutes * 60),
        }

        current_cycle = max(1, int(self.state.cycle or 1))
        current_stage = self.state.stage
        try:
            current_index = stage_order.index(current_stage)
        except ValueError:
            current_index = -1 if current_stage in {"INIT", "PREFLIGHT"} else len(stage_order)

        remaining = 0
        for cycle in range(current_cycle, self.cfg.cycles + 1):
            for index, stage in enumerate(stage_order):
                if stage not in timed:
                    continue
                if stage == "DEGASSING" and cycle != 1:
                    continue
                if cycle == current_cycle and index < current_index:
                    continue
                if (
                    cycle == current_cycle
                    and stage == current_stage
                    and self.state.phase_remaining_s is not None
                ):
                    remaining += max(0, int(self.state.phase_remaining_s))
                else:
                    remaining += timed[stage]
        return remaining

    def _overall_progress_percent(self) -> float:
        stage_order = [
            "DEGASSING",
            "OPEN_VALVE",
            "PRESSURE_CONDITIONING",
            "COSCON_ACTIVATION",
            "SPUTTERING",
            "COSCON_STANDBY",
            "CLOSE_VALVE",
            "ANNEAL_RAMP",
            "ANNEAL_HOLD",
            "ANNEAL_RESET",
        ]
        if self.state.stage == "DONE":
            return 100.0
        if self.state.stage == "ABORTED":
            completed_cycles = max(0, int(self.state.cycle) - 1)
            return 100.0 * completed_cycles / max(1, self.cfg.cycles)

        cycle = max(1, int(self.state.cycle or 1))
        try:
            stage_fraction = stage_order.index(self.state.stage) / len(stage_order)
        except ValueError:
            stage_fraction = 0.0
        completed = max(0.0, (cycle - 1) + stage_fraction)
        return max(0.0, min(99.0, 100.0 * completed / max(1, self.cfg.cycles)))

    def _update_ui_status(self) -> None:
        if self.ui.is_running():
            snap = self.state.snapshot()
            title, description = self._stage_title_description(snap["stage"])
            snap.update(
                {
                    "run_name": self.cfg.run_name,
                    "total_cycles": self.cfg.cycles,
                    "stage_title": title,
                    "stage_description": description,
                    "pressure_warning_mbar": self.cfg.pressure_warning_mbar,
                    "coscon_target_energy_v": self.cfg.coscon_energy_v,
                    "coscon_target_emission_a": self.cfg.coscon_emission_a,
                    "coscon_emission_tolerance_a": self.cfg.coscon_emission_tolerance_a,
                    "coscon_emission_fault_samples": self.cfg.coscon_emission_fault_samples,
                    "start_without_degassing": self.cfg.start_without_degassing,
                    "degas_timeout_s": int(self.cfg.degas_timeout_minutes * 60),
                    "pressure_conditioning_total_s": int(self.cfg.standby_conditioning_s),
                    "operate_timeout_s": int(self.cfg.operate_transition_timeout_s),
                    "sputter_total_s": int(self.cfg.sputter_minutes * 60),
                    "anneal_hold_total_s": int(self.cfg.anneal_hold_minutes * 60),
                    "run_elapsed_s": max(0, int(time.time() - self.run_started_at)),
                    "stage_elapsed_s": max(0, int(time.time() - self.stage_started_at)),
                    "estimated_timed_remaining_s": self._estimated_timed_remaining_s(),
                    "overall_progress_percent": self._overall_progress_percent(),
                }
            )
            self.ui.set_snapshot(snap)

    def _poll_ui_background(self) -> None:
        if self.ui.is_running():
            self.ui.poll_background_tokens()
        self.ui.raise_if_abort_requested()

    def _handle_special_ui_token(self, token: str) -> bool:
        if not token.startswith("pid_sv:"):
            return False
        raw_value = token.split(":", 1)[1].strip().replace(",", ".")
        try:
            target_c = float(raw_value)
        except ValueError:
            message = f"Invalid PID SV value from UI: {raw_value!r}"
            self.state.last_error = message
            self.ui.set_pid_status(message)
            self._update_ui_status()
            return True

        if not (-50.0 <= target_c <= 900.0):
            message = f"Rejected PID SV {target_c:.1f} °C; allowed range is -50 to 900 °C."
            self.state.last_error = message
            self.ui.set_pid_status(message)
            self._update_ui_status()
            return True

        self.ui.set_pid_status(f"Writing PID SV = {target_c:.1f} °C...")
        try:
            self.set_pid_target(target_c)
            self.state.last_error = None
            self.ui.set_pid_status(f"PID SV confirmed at {target_c:.1f} °C.")
        except Exception as exc:
            self.state.last_error = f"Manual PID SV change failed: {exc}"
            self.ui.set_pid_status(self.state.last_error)
            warn(self.state.last_error)
        self._update_ui_status()
        return True

    def _wait_for_token(self, expected: str, message: str) -> None:
        self.ui.wait_for_token([expected], message)

    def _ask_yes_no(self, message: str) -> bool:
        token = self.ui.wait_for_token(["yes", "no"], message)
        return token == "yes"

    def _monitor_loop(self) -> None:
        diagnostic_failures = 0
        diagnostics_enabled = True
        while not self.stop_event.is_set():
            errors: list[str] = []

            try:
                pressure = self.xgs600.read_pressure_mbar()
            except Exception as exc:
                pressure = None
                errors.append(f"Pressure read failed: {exc}")

            try:
                oven_pv = self.pid.read_process_value_c()
            except Exception as exc:
                oven_pv = None
                errors.append(f"PID PV read failed: {exc}")

            try:
                oven_sv = self.pid.read_setpoint_c()
            except Exception as exc:
                oven_sv = None
                errors.append(f"PID SV read failed: {exc}")

            voltage = None
            current = None
            if self.keysight is not None:
                try:
                    voltage, current = self.keysight.read_voltage_current()
                except Exception as exc:
                    errors.append(f"Keysight read failed: {exc}")

            if not self._coscon_polling_paused.is_set():
                try:
                    # Check the pause flag while holding the same lock used by
                    # activation.  This closes the race where a monitor cycle
                    # had already passed the old flag check just before the
                    # activation transaction took ownership of COSCON.
                    with self.coscon.lock:
                        if not self._coscon_polling_paused.is_set():
                            status = self.coscon.status()
                            monitor = self.coscon.monitor()
                            self.state.coscon_mode = status.mode
                            self.state.coscon_interlock = status.interlock
                            self.state.coscon_details = status.details
                            self.state.coscon_energy_v = monitor.energy_v
                            self.state.coscon_emission_a = monitor.emission_a
                            self.state.coscon_filament_a = monitor.filament_a

                            # Diagnostic values are display/logging only. Failure to obtain
                            # them must never abort or mask the primary COSCON safety data.
                            if diagnostics_enabled:
                                try:
                                    diagnostics = self.coscon.diagnostics()
                                    self.state.coscon_energy_current_a = diagnostics.energy_current_a
                                    self.state.coscon_anode_voltage_v = diagnostics.anode_voltage_v
                                    self.state.coscon_repeller_voltage_v = diagnostics.repeller_voltage_v
                                    diagnostic_failures = 0
                                except Exception as diagnostic_exc:
                                    diagnostic_failures += 1
                                    if diagnostic_failures == 1:
                                        warn(f"COSCON diagnostic values unavailable; primary monitoring continues: {diagnostic_exc}")
                                    if diagnostic_failures >= 3:
                                        diagnostics_enabled = False
                                        warn("COSCON diagnostic polling disabled after 3 failures; Energy/Anode/Repeller values will show --.")
                except Exception as exc:
                    errors.append(f"COSCON monitor failed: {exc}")

            self.state.pressure_mbar = pressure
            self.pressure_emergency_alarm.update(pressure)
            self.state.oven_pv_c = oven_pv
            self.state.oven_sv_c = oven_sv
            self.state.keysight_voltage_v = voltage
            self.state.keysight_current_a = current
            self.state.last_error = " | ".join(errors) if errors else None
            self.state.last_update = datetime.now()

            snap = self.state.snapshot()
            self.logger.log_snapshot(snap)
            print(
                f"[{now_str()}] cycle={snap['cycle']} stage={snap['stage']} | "
                f"P={fmt_opt(snap['pressure_mbar'], '.2e')} mbar | "
                f"PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | "
                f"SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C | "
                f"COSCON={snap['coscon_mode'] or '--'} | "
                f"E={fmt_opt(snap['coscon_energy_v'], '.1f')} V | "
                f"Iem={fmt_opt(snap['coscon_emission_a'], '.5f')} A"
            )
            if pressure is not None and pressure == pressure and pressure > self.cfg.pressure_warning_mbar:
                warn(
                    f"Pressure is above warning threshold "
                    f"({pressure:.2e} mbar > {self.cfg.pressure_warning_mbar:.2e} mbar)."
                )
            self._update_ui_status()
            time.sleep(self.cfg.monitor_period_s)

    def preflight(self) -> None:
        self._set_stage("PREFLIGHT", 1)
        banner("SPUTTER-ANNEAL PRE-FLIGHT")
        if not self._ask_yes_no("Have you connected the sputter gun cable?"):
            raise RuntimeError("Run cancelled: sputter gun cable not confirmed.")
        if not self._ask_yes_no("Have you started the sputtering electronics?"):
            raise RuntimeError("Run cancelled: sputtering electronics not confirmed.")
        if self.cfg.start_without_degassing:
            if not self._ask_yes_no(
                "Start without initial Degas is selected. Confirm this is a continuation of the same "
                "chamber preparation and that the operator has verified a new Degas is not required."
            ):
                raise RuntimeError("Run cancelled: skipping Degas was not explicitly confirmed.")
        self._wait_for_token("r", "If everything is ready, click Start run.")

    def start_ui(self) -> None:
        banner("OPENING UNIFIED INTERFACE")
        ok = self.ui.start()
        if ok:
            info("Phase 02 control dashboard started and its Windows backend was verified.")
        else:
            details = self.ui.last_startup_error
            message = (
                "Could not initialize the Phase 02 dashboard Windows backend. "
                "The launcher runtime check should normally repair pywebview/pythonnet automatically."
            )
            if details:
                message += f"\nDashboard startup traceback:\n{details}"
            raise RuntimeError(message)

    def _require_pressure(self, *, normal_window: bool = True) -> float:
        pressure = self.state.pressure_mbar
        if pressure is None or not math.isfinite(pressure):
            raise RuntimeError("Pressure is unavailable; COSCON activation is blocked.")
        if pressure >= self.cfg.pressure_emergency_mbar:
            raise RuntimeError(f"Emergency chamber pressure: {pressure:.3e} mbar")
        if normal_window and not (self.cfg.pressure_min_mbar <= pressure <= self.cfg.pressure_warning_mbar):
            raise RuntimeError(
                f"Pressure {pressure:.3e} mbar is outside the sputtering window "
                f"[{self.cfg.pressure_min_mbar:.1e}, {self.cfg.pressure_warning_mbar:.1e}] mbar."
            )
        return pressure

    def _coscon_safe_stop(self) -> None:
        try:
            status=self.coscon.status()
        except Exception as exc:
            warn(f"COSCON safe-stop status unavailable: {exc}")
            status=None
        if status and status.mode.lower() in {"standby","off"}:
            return
        if status and status.mode.lower()=="degassing":
            try: self.coscon.send("SwitchToOff")
            except Exception as exc: warn(f"COSCON direct Off during Degas failed: {exc}")
            return
        try:
            self.coscon.send("SwitchToStandby")
        except Exception as exc:
            warn(f"COSCON Standby request failed: {exc}")
        deadline=time.time()+self.cfg.standby_timeout_s if hasattr(self.cfg,'standby_timeout_s') else time.time()+25
        while time.time()<deadline:
            try:
                status=self.coscon.status()
                if status.mode.lower() in {"standby","off"}: return
            except Exception: pass
            time.sleep(0.7)
        try: self.coscon.send("SwitchToOff")
        except Exception as exc: warn(f"COSCON Off fallback failed: {exc}")

    def coscon_degassing_step(self, cycle: int) -> None:
        self._set_stage("DEGASSING", cycle)
        banner(f"CYCLE {cycle} - AUTOMATED COSCON DEGAS")
        status=self.coscon.status()
        if status.interlock.lower()!="ok":
            raise RuntimeError(f"COSCON interlock not OK before Degas: {status.raw}")
        if status.mode.lower() not in {"off","standby"}:
            raise RuntimeError(f"Degas requires Off or Standby; current mode={status.mode}")
        self.coscon.send("SwitchToDegas")
        total_s = int(self.cfg.degas_timeout_minutes * 60)
        deadline = time.time() + total_s
        self._set_phase_timer(total_s, total_s, "Degas safety timeout left")
        while time.time() < deadline:
            self._poll_ui_background()
            self._set_phase_timer(
                max(0, int(deadline - time.time())),
                total_s,
                "Degas safety timeout left",
            )
            self._require_pressure(normal_window=False)
            status=self.coscon.status()
            self.state.coscon_mode=status.mode
            self.state.coscon_interlock=status.interlock
            self.state.coscon_details=status.details
            if status.interlock.lower()!="ok":
                raise RuntimeError(f"COSCON interlock changed during Degas: {status.raw}")
            if status.mode.lower()=="error":
                raise RuntimeError(f"COSCON error during Degas: {status.details}")
            if status.mode.lower()=="standby":
                self._clear_phase_timer("Degas complete")
                info("Complete Degas finished naturally in Standby.")
                return
            if status.mode.lower()=="off":
                raise RuntimeError("COSCON returned to Off before natural Degas completion.")
            time.sleep(1.0)
        raise RuntimeError("Timed out waiting for Degas to finish naturally in Standby.")

    def _safe_coscon_modes_before_operate(self, cycle: int) -> set[str]:
        # A continuation run can legitimately begin with COSCON Off because the
        # skipped Degas did not leave it in Standby. Off is an inactive safe
        # state, and this COSCON firmware accepts SwitchToOperate directly from
        # Off. Normal and later cycles still require Standby.
        if cycle == 1 and self.cfg.start_without_degassing:
            return {"off", "standby"}
        return {"standby"}

    def prompt_open_valve(self, cycle: int) -> None:
        self._set_stage("OPEN_VALVE", cycle)
        banner(f"CYCLE {cycle} - OPEN LEAK VALVE")
        allowed_modes = self._safe_coscon_modes_before_operate(cycle)
        initial_status = self.coscon.status()
        if initial_status.interlock.lower() != "ok":
            raise RuntimeError(
                f"COSCON interlock is not OK before opening the leak valve: {initial_status.raw}"
            )
        if initial_status.mode.lower() not in allowed_modes:
            raise RuntimeError(
                "COSCON is not in an inactive safe state before opening the leak valve: "
                f"{initial_status.raw}. Allowed mode(s): {', '.join(sorted(allowed_modes))}."
            )

        self._wait_for_token(
            "o",
            "Open the manual leak valve and stabilize pressure near 2e-5 mbar, "
            "then click 'Valve opened'.",
        )
        self._set_stage("PRESSURE_CONDITIONING", cycle)
        total_s = int(self.cfg.standby_conditioning_s)
        deadline = time.time() + total_s
        self._set_phase_timer(total_s, total_s, "Conditioning left")
        while time.time() < deadline:
            self._poll_ui_background()
            self._set_phase_timer(
                max(0, int(deadline - time.time())),
                total_s,
                "Conditioning left",
            )
            status = self.coscon.status()
            if status.interlock.lower() != "ok" or status.mode.lower() not in allowed_modes:
                raise RuntimeError(
                    "COSCON left its inactive safe state during pressure conditioning: "
                    f"{status.raw}. Allowed mode(s): {', '.join(sorted(allowed_modes))}."
                )
            self._require_pressure(normal_window=True)
            time.sleep(1.0)
        self._set_phase_timer(0, total_s, "Conditioning left")
        info(
            "Pressure conditioning completed with COSCON safely inactive in "
            f"{status.mode}."
        )

    @staticmethod
    def _is_recoverable_activation_overload(status: CosconStatus) -> bool:
        """Return True only for the known transient HV energy overload during activation.

        This deliberately does *not* classify arbitrary COSCON errors as recoverable.
        Interlock must remain OK, and recovery is used only by ``coscon_start_sputter``
        before stable sputtering has begun.
        """
        details = re.sub(r"\s+", " ", (status.details or "").strip().lower())
        return (
            status.mode.lower() == "error"
            and status.interlock.lower() == "ok"
            and "hv-module energy overload" in details
        )

    def _recover_activation_overload(
        self,
        cycle: int,
        status: CosconStatus,
        *,
        retry_number: int,
        total_retries: int,
    ) -> bool:
        note = (
            "Transient COSCON HV-Module Energy Overload during activation. "
            f"8-second automatic retry {retry_number}/{total_retries}: requesting a verified "
            "safe state before retrying Operate. "
            f"COSCON details: {status.details}"
        )
        warn(note)
        self.logger.log_snapshot(self.state.snapshot(), note=note)
        self._set_phase_timer(None, None, "Recovering COSCON")

        # Ask for a safe inactive mode, but do not spend the shutdown path's full
        # timeout here: the requested short recovery window is eight seconds.
        # On this firmware the overload can remain latched in Mode=Error even
        # after measured output has collapsed; that exact result advances to the
        # documented Reset path instead of aborting the whole run prematurely.
        for command in ("SwitchToStandby", "SwitchToOff"):
            try:
                self.coscon.send(command)
            except Exception as exc:
                warn(f"COSCON {command} during short activation recovery: {exc}")

        self.coscon_activation_requested = False
        wait_s = max(0.0, float(self.cfg.coscon_activation_recovery_wait_s))
        deadline = time.time() + wait_s
        self._set_phase_timer(int(math.ceil(wait_s)), int(math.ceil(wait_s)), "Recovery wait")
        final_status = status
        final_monitor = None
        while time.time() < deadline:
            self._poll_ui_background()
            remaining = max(0, int(math.ceil(deadline - time.time())))
            self._set_phase_timer(remaining, int(math.ceil(wait_s)), "Recovery wait")
            self._require_pressure(normal_window=True)
            check = self.coscon.status()
            monitor = self.coscon.monitor()
            final_status = check
            final_monitor = monitor
            if check.interlock.lower() != "ok":
                raise RuntimeError(
                    "COSCON interlock changed during activation recovery: "
                    f"{check.raw}"
                )
            if check.mode.lower() == "error" and not self._is_recoverable_activation_overload(check):
                raise RuntimeError(
                    "COSCON changed to a non-recoverable error during activation recovery: "
                    f"{check.raw}"
                )
            if check.mode.lower() not in {
                "standby", "off", "error", "switchingtostandby", "switchingtooff"
            }:
                raise RuntimeError(
                    "COSCON entered an unexpected mode during activation recovery: "
                    f"{check.raw}"
                )
            time.sleep(0.7)

        if final_status.mode.lower() in {"standby", "off"}:
            self._clear_phase_timer("Recovery complete")
            info(
                "COSCON activation recovery completed in a verified safe state. "
                "Re-validating the requested energy/emission before one retry."
            )
            return True

        safe_energy_limit_v = min(10.0, max(1.0, self.cfg.coscon_energy_tolerance_v))
        safe_emission_limit_a = max(1.0e-4, self.cfg.coscon_emission_tolerance_a)
        if (
            final_monitor is not None
            and self._is_recoverable_activation_overload(final_status)
            and abs(final_monitor.energy_v) <= safe_energy_limit_v
            and abs(final_monitor.emission_a) <= safe_emission_limit_a
        ):
            self._clear_phase_timer("Overload remained latched")
            warn(
                "The 8-second recovery window completed with the exact overload still "
                "latched but output de-energized; advancing to documented Reset recovery."
            )
            return False

        raise RuntimeError(
            "COSCON did not reach a safe inactive state during the 8-second recovery, "
            f"and Reset prerequisites were not met: {final_status.raw}"
        )

    def _recover_activation_overload_with_reset(
        self,
        cycle: int,
        status: CosconStatus,
        *,
        reset_number: int,
        total_resets: int,
    ) -> None:
        """Perform one operator-guarded documented Reset after repeated overload.

        This path is deliberately restricted to the exact activation overload
        recognized by ``_is_recoverable_activation_overload``. The argon valve
        is closed before controller communications are intentionally lost, and
        is reopened only after three consecutive safe Off readings confirm the
        reboot. The normal 60-second pressure conditioning is then repeated.
        """
        if not self._is_recoverable_activation_overload(status):
            raise RuntimeError(
                "COSCON Reset recovery was blocked because the current error is not "
                f"the approved activation overload: {status.raw}"
            )

        note = (
            "COSCON HV-Module Energy Overload repeated after the 8-second retry. "
            f"Starting documented Reset recovery {reset_number}/{total_resets}."
        )
        warn(note)
        self.logger.log_snapshot(self.state.snapshot(), note=note)
        self.coscon_activation_requested = False

        # The fault may remain latched even though output has collapsed. A Reset
        # is allowed only when the interlock is still OK and measured HV/emission
        # are already electrically safe.
        reset_status = self.coscon.status()
        reset_monitor = self.coscon.monitor()
        if reset_status.interlock.lower() != "ok":
            raise RuntimeError(
                "COSCON interlock is not OK before Reset recovery: "
                f"{reset_status.raw}"
            )
        safe_energy_limit_v = min(10.0, max(1.0, self.cfg.coscon_energy_tolerance_v))
        safe_emission_limit_a = max(1.0e-4, self.cfg.coscon_emission_tolerance_a)
        if (
            abs(reset_monitor.energy_v) > safe_energy_limit_v
            or abs(reset_monitor.emission_a) > safe_emission_limit_a
        ):
            raise RuntimeError(
                "COSCON output is not safely de-energized before Reset: "
                f"VEnergy={reset_monitor.energy_v:.3f} V, "
                f"IEmission={reset_monitor.emission_a:.6e} A."
            )

        self._wait_for_token(
            "c",
            "COSCON activation overload repeated. The measured output is de-energized. "
            "Close the manual argon leak valve fully, then click 'Valve closed' to "
            "authorize the documented COSCON Reset.",
        )
        self._require_pressure(normal_window=False)

        try:
            pre_reset_uptime = self.coscon.uptime_s()
        except Exception as exc:
            pre_reset_uptime = None
            warn(f"COSCON pre-Reset uptime was unavailable: {exc}")

        was_polling_paused = self._coscon_polling_paused.is_set()
        self._coscon_polling_paused.set()
        self.state.coscon_mode = "Resetting"
        self.state.coscon_interlock = "--"
        self.state.coscon_details = "Documented controller Reset in progress"
        self.state.coscon_energy_v = 0.0
        self.state.coscon_emission_a = 0.0
        self._update_ui_status()

        reset_reply_ok = False
        saw_reboot_gap = False
        safe_samples = 0
        last_recovery_error = "No recovery reading received."
        recovery_started = time.monotonic()
        reconnect_timeout_s = max(10.0, float(self.cfg.coscon_reset_reconnect_timeout_s))
        required_safe_samples = max(1, int(self.cfg.coscon_reset_safe_samples))
        sample_interval_s = max(0.2, float(self.cfg.coscon_reset_safe_sample_interval_s))

        try:
            info("COSCON UDP -> Reset")
            try:
                reset_reply = self.coscon.send("Reset")
                reset_reply_ok = "OK" in reset_reply.upper()
                info(f"COSCON UDP <- {reset_reply}")
            except Exception as exc:
                # Some controllers can reboot before their UDP acknowledgement
                # reaches the PC. The reboot must still be proved below.
                warn(f"No usable Reset acknowledgement; verifying reboot anyway: {exc}")

            initial_wait_deadline = time.monotonic() + 3.0
            self._set_phase_timer(3, int(reconnect_timeout_s), "Reset reboot wait")
            while time.monotonic() < initial_wait_deadline:
                self._poll_ui_background()
                time.sleep(0.1)

            deadline = recovery_started + reconnect_timeout_s
            while time.monotonic() < deadline:
                self._poll_ui_background()
                remaining = max(0, int(math.ceil(deadline - time.monotonic())))
                self._set_phase_timer(remaining, int(reconnect_timeout_s), "Reset reconnect")
                try:
                    recovered_status = self.coscon.status()
                    recovered_monitor = self.coscon.monitor()
                    self.state.coscon_mode = recovered_status.mode
                    self.state.coscon_interlock = recovered_status.interlock
                    self.state.coscon_details = recovered_status.details
                    self.state.coscon_energy_v = recovered_monitor.energy_v
                    self.state.coscon_emission_a = recovered_monitor.emission_a
                    self.state.coscon_filament_a = recovered_monitor.filament_a

                    is_safe = (
                        recovered_status.mode.lower() == "off"
                        and recovered_status.interlock.lower() == "ok"
                        and abs(recovered_monitor.energy_v) <= safe_energy_limit_v
                        and abs(recovered_monitor.emission_a) <= safe_emission_limit_a
                    )
                    if is_safe:
                        safe_samples += 1
                        info(
                            "Safe post-Reset COSCON reading "
                            f"{safe_samples}/{required_safe_samples}: Mode=Off, "
                            f"Interlock=Ok, VEnergy={recovered_monitor.energy_v:.3f} V, "
                            f"IEmission={recovered_monitor.emission_a:.6e} A."
                        )
                        if safe_samples >= required_safe_samples:
                            break
                    else:
                        safe_samples = 0
                        last_recovery_error = (
                            "Unsafe post-Reset state: "
                            f"{recovered_status.raw}; {recovered_monitor.raw}"
                        )
                except Exception as exc:
                    saw_reboot_gap = True
                    safe_samples = 0
                    last_recovery_error = str(exc)
                time.sleep(sample_interval_s)
            else:
                raise RuntimeError(
                    "Timed out waiting for three safe COSCON Off readings after Reset. "
                    f"Last result: {last_recovery_error}"
                )

            try:
                post_reset_uptime = self.coscon.uptime_s()
            except Exception as exc:
                post_reset_uptime = None
                warn(f"COSCON post-Reset uptime was unavailable: {exc}")

            uptime_restarted = (
                pre_reset_uptime is not None
                and post_reset_uptime is not None
                and post_reset_uptime < pre_reset_uptime
            )
            reboot_confirmed = uptime_restarted or (reset_reply_ok and saw_reboot_gap)
            if not reboot_confirmed:
                raise RuntimeError(
                    "COSCON reached safe Off after Reset, but a controller reboot could not "
                    "be confirmed from uptime or the expected communication gap."
                )

            proof = (
                f"uptime {pre_reset_uptime}s -> {post_reset_uptime}s"
                if uptime_restarted
                else "Reset OK plus expected reboot communication gap"
            )
            recovery_note = (
                "Documented COSCON Reset completed: three consecutive safe Off readings; "
                f"reboot proof: {proof}."
            )
            info(recovery_note)
            self.logger.log_snapshot(self.state.snapshot(), note=recovery_note)
        finally:
            # coscon_start_sputter owns the outer pause during the whole
            # activation/recovery sequence.  Do not reopen the polling race
            # between Reset recovery and the final activation attempt.
            if not was_polling_paused:
                self._coscon_polling_paused.clear()

        self._wait_for_token(
            "o",
            "COSCON Reset is complete and safe Off has been verified three times. "
            "Reopen the manual argon leak valve, stabilize pressure near 2e-5 mbar, "
            "then click 'Valve opened'.",
        )

        conditioning_s = max(0.0, float(self.cfg.coscon_post_reset_conditioning_s))
        deadline = time.time() + conditioning_s
        total_conditioning_s = int(math.ceil(conditioning_s))
        self._set_phase_timer(
            total_conditioning_s,
            total_conditioning_s,
            "Post-Reset conditioning",
        )
        while time.time() < deadline:
            self._poll_ui_background()
            self._set_phase_timer(
                max(0, int(math.ceil(deadline - time.time()))),
                total_conditioning_s,
                "Post-Reset conditioning",
            )
            self._require_pressure(normal_window=True)
            check = self.coscon.status()
            if check.interlock.lower() != "ok" or check.mode.lower() != "off":
                raise RuntimeError(
                    "COSCON left safe Off during post-Reset pressure conditioning: "
                    f"{check.raw}"
                )
            time.sleep(1.0)

        self._clear_phase_timer("Post-Reset conditioning complete")
        info(
            "Post-Reset pressure conditioning completed with COSCON in verified Off. "
            "Re-validating targets before the final activation attempt."
        )

    def coscon_start_sputter(self, cycle: int) -> None:
        self._set_stage("COSCON_ACTIVATION", cycle)
        banner(f"CYCLE {cycle} - AUTOMATED COSCON OPERATE")

        activation_note = (
            "COSCON activation ownership acquired: background polling paused; "
            "ValidateOperateTarget -> 1.5 s quiet interval -> SwitchToOperate "
            "will run as one locked transaction."
        )
        info(activation_note)
        self.logger.log_snapshot(self.state.snapshot(), note=activation_note)

        # The direct-IP/browser path does not have a second Python worker
        # issuing status and diagnostic datagrams.  Keep the automated path
        # equivalent by making the full activation/recovery section exclusive.
        self._coscon_polling_paused.set()
        try:
            # Acquire the COSCON lock after pausing polling so any in-flight
            # monitor transaction finishes before activation ownership begins.
            with self.coscon.lock:
                initial_status = self.coscon.status()
            allowed_modes = self._safe_coscon_modes_before_operate(cycle)
            if initial_status.mode.lower() not in allowed_modes or initial_status.interlock.lower() != "ok":
                raise RuntimeError(
                    "Activation requires an inactive safe COSCON mode with Interlock Ok. "
                    f"Allowed mode(s): {', '.join(sorted(allowed_modes))}; received: {initial_status.raw}"
                )

            max_retries = max(0, int(self.cfg.coscon_activation_overload_retries))
            max_resets = max(0, int(self.cfg.coscon_activation_reset_retries))
            total_attempts = 1 + max_retries + max_resets
            retries_used = 0
            resets_used = 0

            for activation_attempt in range(1, total_attempts + 1):
                self._require_pressure(normal_window=True)
                self.coscon_activation_requested = True
                self.coscon.activate(
                    self.cfg.coscon_emission_a,
                    self.cfg.coscon_energy_v,
                    quiet_s=COSCON_ACTIVATION_QUIET_S,
                )

                total_transition_s = int(self.cfg.operate_transition_timeout_s)
                deadline = time.time() + total_transition_s
                self._set_phase_timer(
                    total_transition_s,
                    total_transition_s,
                    "Operate transition",
                )

                recoverable_status = None
                while time.time() < deadline:
                    self._poll_ui_background()
                    self._set_phase_timer(
                        max(0, int(deadline - time.time())),
                        total_transition_s,
                        "Operate transition",
                    )
                    status = self.coscon.status()
                    monitor = self.coscon.monitor()
                    self._require_pressure(normal_window=True)

                    if status.interlock.lower() != "ok":
                        raise RuntimeError(f"COSCON interlock changed: {status.raw}")
                    if status.mode.lower() == "error":
                        if self._is_recoverable_activation_overload(status):
                            recoverable_status = status
                            break
                        raise RuntimeError(f"COSCON device error: {status.details}")
                    if status.mode.lower() == "operating":
                        break
                    time.sleep(0.7)
                else:
                    raise RuntimeError("Timeout waiting for COSCON Operating.")

                if recoverable_status is None and status.mode.lower() == "operating":
                    stability_total_s = 30
                    stability_deadline = time.time() + stability_total_s
                    good_samples = 0
                    self._set_phase_timer(
                        stability_total_s,
                        stability_total_s,
                        "Output verification",
                    )

                    while time.time() < stability_deadline:
                        self._poll_ui_background()
                        self._set_phase_timer(
                            max(0, int(stability_deadline - time.time())),
                            stability_total_s,
                            "Output verification",
                        )
                        status = self.coscon.status()
                        monitor = self.coscon.monitor()
                        self._require_pressure(normal_window=True)

                        if status.interlock.lower() != "ok":
                            raise RuntimeError(f"COSCON interlock changed: {status.raw}")
                        if status.mode.lower() == "error":
                            if self._is_recoverable_activation_overload(status):
                                recoverable_status = status
                                break
                            raise RuntimeError(f"COSCON device error: {status.details}")

                        energy_ok = (
                            abs(monitor.energy_v - self.cfg.coscon_energy_v)
                            <= self.cfg.coscon_energy_tolerance_v
                        )
                        emission_ok = (
                            abs(monitor.emission_a - self.cfg.coscon_emission_a)
                            <= self.cfg.coscon_emission_tolerance_a
                        )
                        good_samples = (
                            good_samples + 1
                            if status.mode.lower() == "operating" and energy_ok and emission_ok
                            else 0
                        )

                        if good_samples >= self.cfg.coscon_stable_samples:
                            self._clear_phase_timer("Output stable")
                            info(
                                "COSCON stable measured output confirmed "
                                f"on activation attempt {activation_attempt}/{total_attempts}."
                            )
                            return
                        time.sleep(0.7)

                    if recoverable_status is None:
                        raise RuntimeError(
                            "Operating reached but stable measured energy/emission was not confirmed."
                        )

                # Only the exact HV energy-overload error is allowed into this branch.
                # First preserve the short 8-second retry. If that same fault repeats,
                # one documented controller Reset is allowed before the final attempt.
                if retries_used < max_retries:
                    retries_used += 1
                    short_retry_ready = self._recover_activation_overload(
                        cycle,
                        recoverable_status,
                        retry_number=retries_used,
                        total_retries=max_retries,
                    )
                    if short_retry_ready:
                        continue
                    recoverable_status = self.coscon.status()

                if resets_used < max_resets:
                    resets_used += 1
                    self._recover_activation_overload_with_reset(
                        cycle,
                        recoverable_status,
                        reset_number=resets_used,
                        total_resets=max_resets,
                    )
                    continue

                raise RuntimeError(
                    "COSCON HV-Module Energy Overload repeated during activation "
                    f"after {activation_attempt} Operate attempt(s); no approved recovery "
                    "remains after the 8-second retry and documented Reset path. "
                    "Last details: "
                    f"{recoverable_status.details if recoverable_status else 'unknown'}"
                )

            raise RuntimeError("COSCON activation ended without a confirmed stable output.")
        finally:
            self._coscon_polling_paused.clear()

    def run_sputter_timer(self, cycle: int) -> None:
        self._set_stage("SPUTTERING", cycle)
        banner(f"CYCLE {cycle} - AUTOMATED SPUTTERING")
        total_s = int(self.cfg.sputter_minutes * 60)
        start = time.time()
        self._set_phase_timer(total_s, total_s, "Sputtering left")
        emission_bad_samples = 0

        target_emission_ma = self.cfg.coscon_emission_a * 1000.0
        tolerance_emission_ma = self.cfg.coscon_emission_tolerance_a * 1000.0
        minimum_emission_ma = target_emission_ma - tolerance_emission_ma
        maximum_emission_ma = target_emission_ma + tolerance_emission_ma

        while True:
            self._poll_ui_background()
            remaining = total_s - int(time.time() - start)
            self._set_phase_timer(remaining, total_s, "Sputtering left")
            if remaining <= 0:
                break

            self._require_pressure(normal_window=True)
            status = self.coscon.status()
            mon = self.coscon.monitor()
            self.state.coscon_mode = status.mode
            self.state.coscon_interlock = status.interlock
            self.state.coscon_details = status.details
            self.state.coscon_energy_v = mon.energy_v
            self.state.coscon_emission_a = mon.emission_a
            self.state.coscon_filament_a = mon.filament_a
            self.state.last_update = datetime.now()

            # Interlock, device mode, energy collapse and energy tolerance remain
            # immediate abort conditions. Only isolated emission spikes are retried.
            if status.interlock.lower() != "ok":
                raise RuntimeError(f"COSCON interlock changed: {status.raw}")
            if status.mode.lower() == "error":
                raise RuntimeError(f"COSCON device error: {status.details}")
            if status.mode.lower() != "operating":
                raise RuntimeError(f"COSCON left Operating: {status.raw}")
            if mon.energy_v < 0.8 * self.cfg.coscon_energy_v:
                raise RuntimeError(f"COSCON energy collapsed to {mon.energy_v:.1f} V")
            if abs(mon.energy_v - self.cfg.coscon_energy_v) > self.cfg.coscon_energy_tolerance_v:
                raise RuntimeError(f"COSCON energy out of tolerance: {mon.energy_v:.1f} V")

            emission_out_of_tolerance = (
                abs(mon.emission_a - self.cfg.coscon_emission_a)
                > self.cfg.coscon_emission_tolerance_a
            )
            if emission_out_of_tolerance:
                emission_bad_samples += 1
                self.state.coscon_emission_a = mon.emission_a
                self.state.coscon_emission_bad_samples = emission_bad_samples
                warning_note = (
                    "COSCON emission outside tolerance: "
                    f"{mon.emission_a * 1000.0:.3f} mA; expected "
                    f"{minimum_emission_ma:.3f}-{maximum_emission_ma:.3f} mA. "
                    f"Consecutive bad reading {emission_bad_samples}/"
                    f"{self.cfg.coscon_emission_fault_samples}; repeating measurement."
                )
                self.logger.log_snapshot(self.state.snapshot(), note=warning_note)
                self._update_ui_status()
                warn(warning_note)
                if emission_bad_samples >= self.cfg.coscon_emission_fault_samples:
                    raise RuntimeError(
                        "COSCON emission remained out of tolerance for "
                        f"{emission_bad_samples} consecutive readings. Last value: "
                        f"{mon.emission_a * 1000.0:.3f} mA"
                    )
                time.sleep(self.cfg.coscon_emission_recheck_s)
                continue

            if emission_bad_samples:
                recovery_note = (
                    "COSCON emission returned inside tolerance: "
                    f"{mon.emission_a * 1000.0:.3f} mA; consecutive warning counter reset."
                )
                self.logger.log_snapshot(self.state.snapshot(), note=recovery_note)
                info(recovery_note)
            emission_bad_samples = 0
            self.state.coscon_emission_bad_samples = 0
            time.sleep(1.0)

        self.state.coscon_emission_bad_samples = 0
        self._set_stage("COSCON_STANDBY", cycle)
        self.coscon.send("SwitchToStandby")
        standby_total_s = 25
        deadline = time.time() + standby_total_s
        self._set_phase_timer(standby_total_s, standby_total_s, "Standby transition")
        while time.time() < deadline:
            self._poll_ui_background()
            self._set_phase_timer(
                max(0, int(deadline - time.time())),
                standby_total_s,
                "Standby transition",
            )
            status = self.coscon.status()
            if status.mode.lower() in {"standby", "off"}:
                self._set_phase_timer(0, standby_total_s, "Standby confirmed")
                info(f"COSCON safe state confirmed after sputtering: {status.mode}")
                self.coscon_activation_requested = False
                return
            time.sleep(0.7)
        raise RuntimeError("Could not confirm COSCON Standby after sputtering.")

    def prompt_close_valve(self, cycle: int) -> None:
        self._set_stage("CLOSE_VALVE", cycle)
        banner(f"CYCLE {cycle} - CLOSE LEAK VALVE")
        self._wait_for_token("c", "COSCON is in Standby. Close the manual leak valve fully, then click 'Valve closed'.")

    def set_pid_target(self, target_c: float) -> None:
        info(f"Setting PID target to {target_c:.0f} °C")
        last_error = None
        for attempt in range(1, 4):
            try:
                ok, status = self.pid.write_setpoint_c(target_c, verify=True)
                if ok:
                    info(f"PID setpoint write OK: {status}")
                    return
                last_error = status
                warn(f"PID setpoint write attempt {attempt}/3 failed for {target_c:.0f} °C: {status}")
            except Exception as exc:
                last_error = exc
                warn(f"PID setpoint write attempt {attempt}/3 raised for {target_c:.0f} °C: {exc}")
            time.sleep(0.8)
        raise RuntimeError(f"PID setpoint write failed for {target_c:.0f} °C after 3 attempts: {last_error}")

    def wait_until_temperature_reached(self, cycle: int, target_c: float) -> None:
        self._set_stage("ANNEAL_RAMP", cycle)
        self._set_phase_timer(None, None, "Ramp target")
        banner(f"CYCLE {cycle} - WAITING FOR STABLE {target_c:.0f} °C")
        stable_since = None
        required_s = max(0.0, float(self.cfg.temperature_stable_duration_s))
        tolerance = abs(float(self.cfg.temperature_reach_tolerance_c))
        while True:
            self._poll_ui_background()
            temp = self.state.oven_pv_c
            sv = self.state.oven_sv_c
            if temp is None:
                stable_since = None
                warn("PV not available yet. Waiting...")
                time.sleep(self.cfg.monitor_period_s)
                continue
            diff = abs(float(temp) - float(target_c))
            inside = diff <= tolerance
            now_mono = time.monotonic()
            if inside:
                if stable_since is None:
                    stable_since = now_mono
            else:
                stable_since = None
            stable_elapsed = 0.0 if stable_since is None else now_mono - stable_since
            self._set_phase_timer(None, None, f"Stable {stable_elapsed:.0f}/{required_s:.0f}s")
            print(
                f"Current oven PV: {temp:.1f} °C | current SV: {fmt_opt(sv, '.1f')} °C | "
                f"target={target_c:.1f} °C | |Δ|={diff:.1f} °C | "
                f"stable={stable_elapsed:.0f}/{required_s:.0f} s"
            )
            if inside and stable_elapsed >= required_s:
                break
            time.sleep(self.cfg.monitor_period_s)
        info(
            f"Oven remained inside {target_c:.0f} ± {tolerance:.1f} °C "
            f"for {required_s:.0f} s."
        )

    def anneal_hold(self, cycle: int) -> None:
        self._set_stage("ANNEAL_HOLD", cycle)
        banner(f"CYCLE {cycle} - EFFECTIVE ANNEAL HOLD")
        total_s = max(0.0, float(self.cfg.anneal_hold_minutes) * 60.0)
        tolerance = abs(float(self.cfg.temperature_reach_tolerance_c))
        target_c = float(self.cfg.anneal_target_c)
        effective_elapsed = 0.0
        last_mono = time.monotonic()
        self._set_phase_timer(int(total_s), int(total_s), "Anneal effective")
        while effective_elapsed < total_s:
            self._poll_ui_background()
            now_mono = time.monotonic()
            dt = max(0.0, now_mono - last_mono)
            last_mono = now_mono
            snap = self.state.snapshot()
            pv = snap['oven_pv_c']
            inside = pv is not None and abs(float(pv) - target_c) <= tolerance
            if inside or not self.cfg.pause_hold_outside_temperature_band:
                effective_elapsed += dt
            remaining = max(0, int(math.ceil(total_s - effective_elapsed)))
            timer_label = "Anneal effective" if inside else "Anneal paused: PV outside band"
            self._set_phase_timer(remaining, int(total_s), timer_label)
            mins, secs = divmod(remaining, 60)
            print(
                f"Effective anneal: {mins:02d}:{secs:02d} remaining | "
                f"PV={fmt_opt(pv, '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C | "
                f"in_band={inside}"
            )
            time.sleep(1)
        self._set_phase_timer(0, int(total_s), "Anneal effective")
        info("Effective anneal hold finished.")

    def reset_pid_after_anneal(self, cycle: int) -> None:
        self._set_stage("ANNEAL_RESET", cycle)
        banner(f"CYCLE {cycle} - RESET PID")
        self.set_pid_target(self.cfg.anneal_reset_c)
        info(f"PID reset to {self.cfg.anneal_reset_c:.0f} °C")

    def run(self) -> None:
        try:
            self.start_ui()
            import threading
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
            self.preflight()
            for cycle in range(1, self.cfg.cycles + 1):
                if cycle == 1 and not self.cfg.start_without_degassing:
                    self.coscon_degassing_step(cycle)
                elif cycle == 1:
                    banner("CYCLE 1 - INITIAL DEGAS SKIPPED")
                    warn(
                        "Initial COSCON Degas was skipped by the launcher setting. "
                        "This run is treated as a continuation of a previously degassed chamber preparation."
                    )
                else:
                    info(
                        f"Cycle {cycle}: skipping degassing confirmation; degassing is only done once before cycle 1."
                    )
                self.prompt_open_valve(cycle)
                self.coscon_start_sputter(cycle)
                self.run_sputter_timer(cycle)
                self.prompt_close_valve(cycle)
                self.set_pid_target(self.cfg.anneal_target_c)
                self.wait_until_temperature_reached(cycle, self.cfg.anneal_target_c)
                self.anneal_hold(cycle)
                self.reset_pid_after_anneal(cycle)
            self._set_stage("DONE", self.cfg.cycles)
            banner("RUN COMPLETED")
            info("All sputter-anneal cycles finished.")
            info(f"Data saved in: {self.output_dir}")
        except KeyboardInterrupt:
            self.aborted = True
            warn("Abort requested.")
            self._set_stage("ABORTED")
        except Exception:
            self.aborted = True
            self._set_stage("ABORTED")
            raise
        finally:
            self.shutdown()

    def _save_final_png_graphs(self) -> None:
        pressure_times = []
        pressure_values = []
        temperature_times = []
        temperature_values = []

        try:
            with open(self.logger.csv_path, "r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    timestamp_text = (row.get("timestamp") or "").strip()
                    try:
                        timestamp = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue

                    pressure = safe_float_from_text(row.get("pressure_mbar") or "")
                    if pressure is not None:
                        pressure_times.append(timestamp)
                        pressure_values.append(pressure)

                    oven_pv = safe_float_from_text(row.get("oven_pv_c") or "")
                    if oven_pv is not None:
                        temperature_times.append(timestamp)
                        temperature_values.append(oven_pv)
        except Exception as exc:
            warn(f"Could not read telemetry CSV for final PNG plots: {exc}")
            return

        def save_xy_plot(times, values, title, ylabel, filename):
            if not times or not values:
                warn(f"Could not save {filename}: no data available.")
                return
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(times, values, linewidth=1.8)
            ax.set_title(title)
            ax.set_xlabel("Time")
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3)
            fig.autofmt_xdate()
            fig.tight_layout()
            path = os.path.join(self.output_dir, filename)
            fig.savefig(path, dpi=150)
            plt.close(fig)
            info(f"Saved final PNG plot: {path}")

        save_xy_plot(
            pressure_times,
            pressure_values,
            "Pressure vs time",
            "Pressure (mbar)",
            "pressure_vs_time.png",
        )
        save_xy_plot(
            temperature_times,
            temperature_values,
            "Oven PID temperature vs time",
            "Temperature (°C)",
            "oven_pid_temperature_vs_time.png",
        )

    def shutdown(self) -> None:
        if self.aborted or self.coscon_activation_requested:
            warn("Shutdown path: requesting a verified COSCON safe state.")
            self._coscon_safe_stop()
        if self.aborted and self.cfg.try_reset_pid_on_abort:
            warn(f"Abort path: trying to set PID setpoint to {self.cfg.abort_reset_c:.0f} °C.")
            reset_ok = False
            last_reset_error = None
            for attempt in range(1, 4):
                try:
                    ok, status = self.pid.write_setpoint_c(self.cfg.abort_reset_c, verify=True)
                    if ok:
                        info(f"Abort path PID reset OK: {status}")
                        reset_ok = True
                        break
                    last_reset_error = status
                    warn(f"Abort path PID reset attempt {attempt}/3 failed: {status}")
                except Exception as exc:
                    last_reset_error = exc
                    warn(f"Abort path PID reset attempt {attempt}/3 raised an exception: {exc}")
                time.sleep(0.8)
            if not reset_ok:
                warn(f"Abort path PID reset did not complete. Last error: {last_reset_error}. Check the PID manually.")

        self.pressure_emergency_alarm.close()
        self.stop_event.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)

        self.logger.close()
        self._save_final_png_graphs()
        self.ui.close()

        for device in (self.keysight, self.pid, self.xgs600):
            if device is None:
                continue
            try:
                device.close()
            except Exception:
                pass

        # Serial release pause after device close. This only helps Windows free
        # COM handles before the unified launcher starts the next phase.
        time.sleep(0.5)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    colorama_init(autoreset=True)
    mp.freeze_support()

    run_name = os.environ.get("NPG_CHAMBER_RUN_NAME", "").strip() or input("Run name [press Enter for default]: ").strip() or "Run"
    cfg = RunConfig(run_name=run_name)
    apply_overrides_to_object("sputter", cfg, RUN_AUTOMATION_OVERRIDES)

    controller = SputterAnnealController(cfg)
    controller.run()


if __name__ == "__main__":
    main()
