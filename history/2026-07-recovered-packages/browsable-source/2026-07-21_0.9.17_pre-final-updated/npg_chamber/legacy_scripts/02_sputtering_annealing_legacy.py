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
import time


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
from html import unescape
from typing import Optional, Callable

from npg_chamber.config.run_parameters import (
    apply_overrides_to_object,
    format_override_summary,
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
    sputter_minutes: int = 20
    anneal_target_c: float = 620.0
    anneal_hold_minutes: int = 10
    anneal_reset_c: float = 0.0
    abort_reset_c: float = 20.0

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
    coscon_energy_v: float = 2250.0
    coscon_emission_a: float = 0.010
    coscon_energy_tolerance_v: float = 50.0
    coscon_emission_tolerance_a: float = 0.001
    coscon_stable_samples: int = 5
    pressure_min_mbar: float = 1.0e-5
    pressure_emergency_mbar: float = 1.0e-4

    # Pressure guidance
    target_ar_pressure_mbar: float = 2.0e-5
    pressure_warning_mbar: float = 5.0e-5

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
    stable_temperature_reads: int = 3

    # Abort behaviour
    try_reset_pid_on_abort: bool = True


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


def try_extract_html_title(text: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = unescape(m.group(1)).strip()
    title = re.sub(r"\s+", " ", title)
    return title or None

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
    Create a short output folder under:
        Data Samples/Sputtering-Annealing Data/

    The final run folder is still named like:
        "<input name> Sputtering-Annealing"
    If it already exists, a short numeric suffix is added.
    """
    safe = re.sub(r'[<>:"/\\|?*]+', "_", base_name).strip()
    safe = re.sub(r"\s+", " ", safe)
    if not safe:
        safe = "Run"
    base_root = _resolve_phase_data_parent("Sputtering-Annealing Data")
    root = os.path.join(base_root, f"{safe} Sputtering-Annealing")
    if not os.path.exists(root):
        return root

    counter = 2
    while True:
        candidate = os.path.join(base_root, f"{safe} Sputtering-Annealing {counter}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1



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
        "SwitchToDegas",
        "SwitchToStandby",
        "SwitchToOff",
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
        r"\b([A-Za-z][A-Za-z0-9]*)="
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

    def send(self, command: str) -> str:
        if not self._allowed(command):
            raise RuntimeError(f"Blocked COSCON command: {command!r}")

        with self.lock:
            payload = (command + "\r").encode("ascii")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout_s)
                sock.sendto(payload, (self.ip, self.port))
                try:
                    data, sender = sock.recvfrom(8192)
                except socket.timeout as exc:
                    raise RuntimeError(
                        f"No COSCON reply to {command!r} within "
                        f"{self.timeout_s:.1f} s."
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
      --bg:#f4f7fb;
      --surface:#ffffff;
      --surface-soft:#f8fafc;
      --border:#d9e3ee;
      --text:#22364c;
      --muted:#697d93;
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
                Degas, target validation, Operate, output qualification, sputtering timing and return to Standby are automated. The leak valve remains manual.<br><br>
                Supervise the first complete three-cycle runs and keep local COSCON controls accessible.
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
    {key:'PRESSURE_CONDITIONING', name:'Pressure stabilization', desc:'COSCON remains in Standby', time:'60 s'},
    {key:'COSCON_ACTIVATION', name:'COSCON activation', desc:'Validate + Operate + output verification', time:'~10–35 s'},
    {key:'SPUTTERING', name:'Sputtering', desc:'Continuous output and pressure checks', time:'recipe timer'},
    {key:'COSCON_STANDBY', name:'Return to Standby', desc:'Automatic safe-state verification', time:'≤25 s'},
    {key:'CLOSE_VALVE', name:'Close argon valve', desc:'After automatic Standby', time:'manual'},
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

    WORKFLOW_STEPS.forEach((step, idx) => {
      const row = document.createElement('div');
      row.className = 'step';
      if (idx < currentIndex) row.classList.add('done');
      if (idx === currentIndex) row.classList.add('active');
      if (idx === 0 && cycle > 1) {
        row.classList.remove('done', 'active');
        row.classList.add('skipped');
      }

      const number = document.createElement('div');
      number.className = 'stepNumber';
      number.textContent = (idx === 0 && cycle > 1) ? '—' : String(idx + 1);

      const text = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'stepName';
      name.textContent = step.name;
      const desc = document.createElement('div');
      desc.className = 'stepDesc';
      desc.textContent = (idx === 0 && cycle > 1) ? 'Skipped after cycle 1' : step.desc;
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
    el('emissionSub').textContent = `Target: ${fmtNumber(Number(snap.coscon_target_emission_a) * 1000, 3)} mA`;
    el('filamentVal').textContent = `${fmtNumber(snap.coscon_filament_a, 3)} A`;

    const pressure = Number(snap.pressure_mbar);
    el('pressureVal').textContent = Number.isNaN(pressure) ? '--' : pressure.toExponential(3);
    const pressureHigh = !Number.isNaN(pressure) && pressure > Number(snap.pressure_warning_mbar || 5e-5);
    el('pressureVal').className = `metricValue ${pressureHigh ? 'bad' : ''}`;
    el('pressureSub').textContent = pressureHigh
      ? `Above warning limit ${Number(snap.pressure_warning_mbar || 5e-5).toExponential(1)} mbar`
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


def _unified_ui_target(command_q: mp.Queue, event_q: mp.Queue, title: str, width: int, height: int) -> None:
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
    webview.start(debug=False)


class UnifiedUIClient:
    def __init__(self, enabled: bool, title: str, width: int, height: int) -> None:
        self.enabled = enabled
        self.title = title
        self.width = width
        self.height = height
        self.process: Optional[mp.Process] = None
        self.command_q: Optional[mp.Queue] = None
        self.event_q: Optional[mp.Queue] = None
        self.special_token_handler: Optional[Callable[[str], bool]] = None

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
        self.process = mp.Process(
            target=_unified_ui_target,
            args=(self.command_q, self.event_q, self.title, self.width, self.height),
            daemon=True,
        )
        self.process.start()
        return True

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
                raise KeyboardInterrupt("Abort requested from unified UI")
            self._handle_special_token(token)

    def wait_for_token(self, allowed: list[str], message: str) -> str:
        if not self.is_running():
            raise RuntimeError("Unified UI is not running")
        self.command_q.put(("prompt", {"allowed": allowed, "message": message}))
        while True:
            kind, token = self.event_q.get()
            if kind != "token":
                continue
            if token == "abort":
                raise KeyboardInterrupt("Abort requested from unified UI")
            if self._handle_special_token(token):
                continue
            if token in allowed:
                return token

    def close(self) -> None:
        if self.is_running():
            self.command_q.put(("close", None))
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2)


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

        base_dir = make_short_output_dir(self.cfg.run_name)
        self.logger = DataLogger(base_dir)
        self.output_dir = base_dir
        try:
            parameter_record_path = write_effective_parameters(
                os.path.join(self.output_dir, "automation_parameters.json"),
                "sputter",
                RUN_AUTOMATION_OVERRIDES,
            )
            info(f"Saved effective automation parameters: {parameter_record_path}")
        except Exception as exc:
            warn(f"Could not save effective automation parameters: {exc}")

        self.xgs600 = XGS600Gauge(self.cfg.xgs600_port, self.cfg.xgs600_baud)
        self.pid = OvenPID(self.cfg.pid_port, self.cfg.pid_baud, self.cfg.pid_address)
        self.keysight = KeysightSupply(self.cfg.keysight_port, self.cfg.keysight_baud) if self.cfg.keysight_port else None
        self.coscon = CosconUDP(self.cfg.coscon_ip, self.cfg.coscon_udp_port, self.cfg.coscon_udp_timeout_s)
        self.coscon_activation_requested = False

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
            "PRESSURE_CONDITIONING": ("Pressure conditioning", "COSCON remains in Standby while pressure stability is verified."),
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
        if self.ui.is_running():
            self.ui.wait_for_token([expected], message)
            return
        # fallback console
        while True:
            reply = input(f"{message} Type '{expected}': ").strip().lower()
            if reply == expected:
                return

    def _ask_yes_no(self, message: str) -> bool:
        if self.ui.is_running():
            token = self.ui.wait_for_token(["yes", "no"], message)
            return token == "yes"
        reply = input(f"{message} [y/N]: ").strip().lower()
        return reply in ("y", "yes")

    def _monitor_loop(self) -> None:
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

            try:
                status = self.coscon.status()
                monitor = self.coscon.monitor()
                self.state.coscon_mode = status.mode
                self.state.coscon_interlock = status.interlock
                self.state.coscon_details = status.details
                self.state.coscon_energy_v = monitor.energy_v
                self.state.coscon_emission_a = monitor.emission_a
                self.state.coscon_filament_a = monitor.filament_a
            except Exception as exc:
                errors.append(f"COSCON monitor failed: {exc}")

            self.state.pressure_mbar = pressure
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
        self._wait_for_token("r", "If everything is ready, click Start run.")

    def start_ui(self) -> None:
        banner("OPENING UNIFIED INTERFACE")
        ok = self.ui.start()
        if ok:
            info("Phase 02 control dashboard started.")
        else:
            raise RuntimeError(
                "Could not start the Phase 02 dashboard. Install pywebview and WebView2 Runtime."
            )

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
            pressure = self._require_pressure(normal_window=False)
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

    def prompt_open_valve(self, cycle: int) -> None:
        self._set_stage("OPEN_VALVE", cycle)
        banner(f"CYCLE {cycle} - OPEN LEAK VALVE")
        self._wait_for_token("o", "Open the manual leak valve and stabilize pressure near 2e-5 mbar, then click 'Valve opened'.")
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
            if status.mode.lower()!="standby" or status.interlock.lower()!="ok":
                raise RuntimeError(f"COSCON must remain in Standby during pressure conditioning: {status.raw}")
            self._require_pressure(normal_window=True)
            time.sleep(1.0)
        self._set_phase_timer(0, total_s, "Conditioning left")
        info("Pressure and Standby conditioning completed.")

    def coscon_start_sputter(self, cycle: int) -> None:
        self._set_stage("COSCON_ACTIVATION", cycle)
        banner(f"CYCLE {cycle} - AUTOMATED COSCON OPERATE")

        status = self.coscon.status()
        if status.mode.lower() != "standby" or status.interlock.lower() != "ok":
            raise RuntimeError(
                f"Activation requires Standby/Interlock Ok: {status.raw}"
            )

        self._require_pressure(normal_window=True)
        self.coscon.validate(
            self.cfg.coscon_emission_a,
            self.cfg.coscon_energy_v,
        )
        self.coscon_activation_requested = True
        self.coscon.operate(
            self.cfg.coscon_emission_a,
            self.cfg.coscon_energy_v,
        )

        total_transition_s = int(self.cfg.operate_transition_timeout_s)
        deadline = time.time() + total_transition_s
        self._set_phase_timer(
            total_transition_s,
            total_transition_s,
            "Operate transition",
        )

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
                raise RuntimeError(f"COSCON device error: {status.details}")
            if status.mode.lower() == "operating":
                break
            time.sleep(0.7)
        else:
            raise RuntimeError("Timeout waiting for COSCON Operating.")

        stability_total_s = 20
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
                info("COSCON stable measured output confirmed.")
                return
            time.sleep(0.7)

        raise RuntimeError(
            "Operating reached but stable measured energy/emission was not confirmed."
        )

    def run_sputter_timer(self, cycle: int) -> None:
        self._set_stage("SPUTTERING", cycle)
        banner(f"CYCLE {cycle} - AUTOMATED SPUTTERING")
        total_s=int(self.cfg.sputter_minutes*60); start=time.time(); self._set_phase_timer(total_s,total_s,"Sputtering left")
        while True:
            self._poll_ui_background(); remaining=total_s-int(time.time()-start); self._set_phase_timer(remaining,total_s,"Sputtering left")
            if remaining<=0: break
            pressure=self._require_pressure(normal_window=True); status=self.coscon.status(); mon=self.coscon.monitor()
            if status.interlock.lower()!="ok": raise RuntimeError(f"COSCON interlock changed: {status.raw}")
            if status.mode.lower()=="error": raise RuntimeError(f"COSCON device error: {status.details}")
            if status.mode.lower()!="operating": raise RuntimeError(f"COSCON left Operating: {status.raw}")
            if mon.energy_v < 0.8*self.cfg.coscon_energy_v:
                raise RuntimeError(f"COSCON energy collapsed to {mon.energy_v:.1f} V")
            if abs(mon.energy_v-self.cfg.coscon_energy_v)>self.cfg.coscon_energy_tolerance_v:
                raise RuntimeError(f"COSCON energy out of tolerance: {mon.energy_v:.1f} V")
            if abs(mon.emission_a-self.cfg.coscon_emission_a)>self.cfg.coscon_emission_tolerance_a:
                raise RuntimeError(f"COSCON emission out of tolerance: {mon.emission_a*1000:.3f} mA")
            time.sleep(1.0)
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
        banner(f"CYCLE {cycle} - WAITING FOR {target_c:.0f} °C")
        stable_hits = 0
        while stable_hits < self.cfg.stable_temperature_reads:
            self._poll_ui_background()
            temp = self.state.oven_pv_c
            sv = self.state.oven_sv_c
            if temp is None:
                warn("PV not available yet. Waiting...")
                time.sleep(2)
                continue
            diff = abs(temp - target_c)
            self._set_phase_timer(None, None, f"Ramp Δ {diff:.1f}°C")
            print(
                f"Current oven PV: {temp:.1f} °C | current SV: {fmt_opt(sv, '.1f')} °C | target={target_c:.1f} °C | diff={diff:.1f} °C"
            )
            if temp >= (target_c - self.cfg.temperature_reach_tolerance_c):
                stable_hits += 1
            else:
                stable_hits = 0
            time.sleep(self.cfg.monitor_period_s)
        info(f"Oven reached target window near {target_c:.0f} °C.")

    def anneal_hold(self, cycle: int) -> None:
        self._set_stage("ANNEAL_HOLD", cycle)
        banner(f"CYCLE {cycle} - ANNEAL HOLD")
        total_s = int(self.cfg.anneal_hold_minutes * 60)
        self._set_phase_timer(total_s, total_s, "Anneal left")
        start = time.time()
        while True:
            self._poll_ui_background()
            elapsed = int(time.time() - start)
            remaining = total_s - elapsed
            self._set_phase_timer(remaining, total_s, "Anneal left")
            if remaining <= 0:
                break
            mins, secs = divmod(remaining, 60)
            snap = self.state.snapshot()
            print(
                f"Anneal hold countdown: {mins:02d}:{secs:02d} remaining | PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C"
            )
            time.sleep(1)
        self._set_phase_timer(0, total_s, "Anneal left")
        info("Anneal hold finished.")

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
                if cycle == 1:
                    self.coscon_degassing_step(cycle)
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
    print("\n" + format_override_summary("sputter", RUN_AUTOMATION_OVERRIDES) + "\n")

    print("\nCurrent configuration:")
    print(f"  COSCON UDP: {cfg.coscon_ip}:{cfg.coscon_udp_port}")
    print("  COSCON web interface: not used; keep it closed")
    print(f"  Cycles: {cfg.cycles}")
    print(f"  Sputter time: {cfg.sputter_minutes} min")
    print(f"  COSCON energy target: {cfg.coscon_energy_v:.1f} V")
    print(f"  COSCON emission target: {cfg.coscon_emission_a * 1000:.3f} mA")
    print(f"  Anneal target: {cfg.anneal_target_c:.0f} °C")
    print(f"  Anneal hold: {cfg.anneal_hold_minutes} min")
    print(f"  Anneal reset after cycle: {cfg.anneal_reset_c:.0f} °C")
    print(f"  Abort reset: {cfg.abort_reset_c:.0f} °C")
    print(f"  PID port/address: {cfg.pid_port} / {cfg.pid_address}")
    print("  PID serial lock/retries: enabled")
    print("\nRequirements for the unified UI on Windows:")
    print("  pip install pyserial colorama pywebview")
    print("  and make sure Microsoft Edge WebView2 Runtime is installed")

    controller = SputterAnnealController(cfg)
    controller.run()


if __name__ == "__main__":
    main()
