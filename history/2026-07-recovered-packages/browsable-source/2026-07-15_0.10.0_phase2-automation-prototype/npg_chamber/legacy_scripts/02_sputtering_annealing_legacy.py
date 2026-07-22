"""
Sputtering-Annealings Controller v11.0
=====================================

Phase 02 now controls the SPECS COSCON IS directly through its documented UDP
protocol instead of embedding the COSCON web interface.

Automated actions
-----------------
- Complete initial COSCON Degas from Off and wait for its natural Standby state.
- Validate and activate the 10 mA / 2250 V sputtering target through UDP.
- Verify Mode=Operating plus measured energy, emission, pressure and interlock.
- Monitor those safety signals for the complete sputtering countdown.
- Change Operating -> Standby automatically before each annealing ramp.
- Write and verify the oven PID setpoint, wait for temperature and run the
  annealing hold as in the previous Phase 02.

Operator actions that remain manual
-----------------------------------
- Connect/check the sputter-gun cable and switch on the electronics.
- Open and close the argon leak valve.  The script verifies pressure after the
  valve is opened and does not start sputtering until pressure is stable.
- Keep physical supervision and local COSCON controls available.

Important safety behaviour
--------------------------
- The COSCON web interface and Prodigy/SpecsLab must remain closed while this
  phase is running so only one client issues control commands.
- Automatic COSCON operation blocks on missing, stale, NaN or unsafe pressure.
- A device Error, interlock change, major energy collapse, pressure emergency,
  communication failure after activation or operator abort triggers a best-
  effort Standby request, with Off used only when a safe state cannot be
  confirmed.
- Reset, network changes and preset-write/delete commands are not implemented.
- The leak valve is not motor-controlled by this version.
"""


from __future__ import annotations

import csv
import math
import multiprocessing as mp
import os
import queue
import re
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
from typing import Optional, Callable

from npg_chamber.devices.coscon_udp import (
    COSCONCommunicationError,
    COSCONMonitorValues,
    COSCONStatus,
    COSCONUDPClient,
)
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

    # COSCON UDP control. Network settings are fixed hardware configuration and
    # are intentionally not exposed in the run-only parameter editor.
    coscon_ip: str = "192.168.236.186"
    coscon_udp_port: int = 2005
    coscon_udp_timeout_s: float = 2.0
    coscon_window_title: str = "Sputtering-Annealings Controller v11.0"
    coscon_window_width: int = 1500
    coscon_window_height: int = 950

    # Validated sputtering recipe.
    coscon_emission_target_a: float = 0.010
    coscon_energy_target_v: float = 2250.0
    coscon_energy_tolerance_v: float = 50.0
    coscon_emission_tolerance_a: float = 0.001
    coscon_stable_samples: int = 5
    coscon_operate_timeout_s: float = 35.0
    coscon_degas_timeout_minutes: float = 20.0
    coscon_standby_timeout_s: float = 25.0
    coscon_off_timeout_s: float = 25.0
    coscon_poll_s: float = 0.60

    # Pressure safety. The emergency Degas threshold is fixed in this source;
    # normal recipe values can be changed for one launcher session.
    target_ar_pressure_mbar: float = 2.0e-5
    pressure_min_mbar: float = 1.0e-5
    pressure_warning_mbar: float = 5.0e-5
    pressure_stable_seconds: float = 60.0
    pressure_stable_relative_band: float = 0.08
    degas_start_max_pressure_mbar: float = 1.0e-5
    degas_abort_pressure_mbar: float = 1.0e-4

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
    coscon_mode: str = "UNKNOWN"
    coscon_interlock: str = "UNKNOWN"
    coscon_details: str = ""
    coscon_target_energy_v: Optional[float] = None
    coscon_target_emission_a: Optional[float] = None
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
            "coscon_target_energy_v": self.coscon_target_energy_v,
            "coscon_target_emission_a": self.coscon_target_emission_a,
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
        self._lock = threading.RLock()
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
                "coscon_target_energy_v",
                "coscon_target_emission_a",
                "coscon_energy_v",
                "coscon_emission_a",
                "coscon_filament_a",
                "note",
            ]
        )
        self._fh.flush()

    def log_snapshot(self, snap: dict, note: str = "") -> None:
        with self._lock:
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
                    snap["coscon_target_energy_v"],
                    snap["coscon_target_emission_a"],
                    snap["coscon_energy_v"],
                    snap["coscon_emission_a"],
                    snap["coscon_filament_a"],
                    note,
                ]
            )
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


# =============================================================================
# DEVICES
# =============================================================================

class XGS600Gauge:
    def __init__(self, port: str, baud: int, timeout: float = 1.0) -> None:
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)
        self.lock = threading.RLock()

    def read_pressure_mbar(self, *, strict: bool = False) -> float:
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(b"#0002USYNTH\r")
            self.ser.flush()
            time.sleep(0.12)
            deadline = time.monotonic() + max(0.5, float(self.ser.timeout or 1.0))
            buffer = bytearray()
            last_data_at = None
            while time.monotonic() < deadline:
                waiting = self.ser.in_waiting
                if waiting:
                    buffer.extend(self.ser.read(waiting))
                    last_data_at = time.monotonic()
                elif buffer and last_data_at is not None and time.monotonic() - last_data_at >= 0.08:
                    break
                time.sleep(0.02)
            if not buffer:
                buffer.extend(self.ser.read(100))

        message = bytes(buffer).decode(errors="ignore").strip().lstrip(">").strip()
        if message.lower() in {"", "nan", "+nan", "-nan"}:
            if strict:
                raise RuntimeError(f"XGS600 pressure unavailable: {message!r}")
            return float("nan")
        value = safe_float_from_text(message)
        if value is None or not math.isfinite(value) or value <= 0:
            if strict:
                raise RuntimeError(f"Unsafe/unparseable XGS600 pressure: {message!r}")
            return float("nan")
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


# =============================================================================
# UNIFIED UI
# =============================================================================

HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Sputtering-Annealings Controller</title>
  <style>
    :root{
      --bg1:#08101c;
      --bg2:#0d1726;
      --panel:#111b2d;
      --border:#263751;
      --text:#edf4ff;
      --muted:#99abc7;
      --shadow:0 16px 40px rgba(0,0,0,.28);
      --radius:20px;

      --themeA:#60a5fa;
      --themeB:#2563eb;
      --themeGlow:rgba(96,165,250,.20);
    }

    * { box-sizing:border-box; }
    html, body {
      margin:0; padding:0; height:100%;
      font-family:Segoe UI, Inter, Arial, sans-serif;
      background:
        radial-gradient(circle at top left, var(--themeGlow), transparent 28%),
        radial-gradient(circle at top right, rgba(255,255,255,.03), transparent 22%),
        linear-gradient(180deg, var(--bg1) 0%, var(--bg2) 100%);
      color:var(--text);
      transition: background .25s ease;
    }

    body.theme-init      { --themeA:#60a5fa; --themeB:#2563eb; --themeGlow:rgba(96,165,250,.20); }
    body.theme-degassing { --themeA:#a78bfa; --themeB:#7c3aed; --themeGlow:rgba(167,139,250,.20); }
    body.theme-sputter   { --themeA:#f59e0b; --themeB:#b45309; --themeGlow:rgba(245,158,11,.20); }
    body.theme-anneal    { --themeA:#ef4444; --themeB:#b91c1c; --themeGlow:rgba(239,68,68,.18); }
    body.theme-standby   { --themeA:#38bdf8; --themeB:#0369a1; --themeGlow:rgba(56,189,248,.20); }
    body.theme-done      { --themeA:#22c55e; --themeB:#15803d; --themeGlow:rgba(34,197,94,.18); }
    body.theme-abort     { --themeA:#fb7185; --themeB:#be123c; --themeGlow:rgba(251,113,133,.18); }

    .app {
      height:100%;
      display:grid;
      grid-template-columns: minmax(760px, 1.7fr) minmax(400px, .9fr);
      gap:12px;
      padding:10px;
    }

    .card {
      background:linear-gradient(180deg, rgba(22,35,56,.96), rgba(17,27,45,.98));
      border:1px solid var(--border);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      overflow:hidden;
      min-height:0;
      backdrop-filter: blur(8px);
    }

    .left.card { display:flex; flex-direction:column; }
    .header {
      display:flex; align-items:center; justify-content:space-between;
      padding:16px 18px;
      border-bottom:1px solid rgba(255,255,255,.06);
      background:
        linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,0)),
        linear-gradient(90deg, rgba(255,255,255,.01), transparent 45%);
    }
    .titleWrap { display:flex; flex-direction:column; gap:5px; }
    .title { font-size:19px; font-weight:800; letter-spacing:.2px; }
    .subtitle { font-size:12px; color:var(--muted); }

    .statusCluster {
      display:flex;
      align-items:center;
      justify-content:flex-end;
      gap:10px;
      min-width:310px;
    }

    .pressureWarning {
      display:none;
      padding:8px 13px;
      border-radius:999px;
      font-size:11px;
      font-weight:950;
      letter-spacing:.25px;
      color:#fff;
      background:linear-gradient(180deg, #ef4444 0%, #991b1b 100%);
      box-shadow:0 0 0 2px rgba(239,68,68,.25), 0 10px 26px rgba(153,27,27,.35);
      white-space:nowrap;
    }

    .statusBadge {
      padding:8px 13px;
      border-radius:999px;
      font-size:10px;
      font-weight:800;
      letter-spacing:.5px;
      text-transform:uppercase;
      color:#fff;
      background:linear-gradient(180deg, var(--themeA), var(--themeB));
      box-shadow:0 8px 22px rgba(0,0,0,.22);
      white-space:nowrap;
    }

    .timerBadge {
      min-width:118px;
      padding:7px 11px;
      border-radius:16px;
      text-align:right;
      background:linear-gradient(180deg, rgba(255,255,255,.075), rgba(255,255,255,.03));
      border:1px solid rgba(255,255,255,.09);
      box-shadow:0 8px 20px rgba(0,0,0,.18);
    }
    .timerBadge span {
      display:block;
      color:var(--muted);
      font-size:9px;
      text-transform:uppercase;
      letter-spacing:.65px;
      font-weight:800;
      margin-bottom:2px;
    }
    .timerBadge strong {
      display:block;
      color:#fff;
      font-size:17px;
      line-height:1;
      font-weight:950;
      letter-spacing:.3px;
    }

    .cosconNative {
      flex:1;
      padding:16px;
      overflow:auto;
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      align-content:start;
      gap:12px;
    }
    .cosconHero {
      grid-column:1/3;
      padding:18px;
      border-radius:18px;
      background:linear-gradient(180deg, rgba(96,165,250,.13), rgba(96,165,250,.035));
      border:1px solid rgba(96,165,250,.22);
    }
    .cosconHero strong { display:block; font-size:31px; margin:5px 0; }
    .cosconHero small { color:var(--muted); }
    .cosconMetric {
      padding:14px;
      border-radius:16px;
      background:linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.025));
      border:1px solid rgba(255,255,255,.08);
    }
    .cosconMetric span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.65px; font-weight:800; }
    .cosconMetric strong { display:block; font-size:21px; margin-top:6px; }
    .cosconDetails {
      grid-column:1/3;
      padding:13px;
      min-height:52px;
      border-radius:16px;
      color:#dbeafe;
      background:rgba(4,12,24,.38);
      border:1px solid rgba(255,255,255,.07);
      white-space:pre-wrap;
      line-height:1.4;
    }

    .right {
      display:grid;
      grid-template-rows:auto auto auto auto;
      gap:8px;
      min-height:0;
      overflow-y:auto;
    }

    .section { padding:10px; }
    .sectionTitle {
      display:flex; align-items:center; justify-content:space-between;
      font-size:14px;
      font-weight:800;
      margin-bottom:8px;
      letter-spacing:.2px;
    }

    .accentDot {
      width:10px; height:10px; border-radius:999px;
      background:linear-gradient(180deg, var(--themeA), var(--themeB));
      box-shadow:0 0 0 6px rgba(255,255,255,.03), 0 0 18px var(--themeGlow);
    }

    .promptBox {
      min-height:56px;
      line-height:1.35;
      color:var(--text);
      font-size:12px;
      padding:9px;
      border-radius:16px;
      background:linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.025));
      border:1px solid rgba(255,255,255,.07);
      white-space:pre-line;
    }

    .buttonGrid {
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:6px;
      margin-top:6px;
    }

    button {
      padding:8px 9px;
      border:1px solid rgba(255,255,255,.08);
      border-radius:13px;
      background:linear-gradient(180deg, #26354b 0%, #1b2636 100%);
      color:#f8fbff;
      font-weight:800;
      cursor:pointer;
      transition:transform .08s ease, filter .15s ease, border-color .15s ease, box-shadow .15s ease;
      box-shadow:0 8px 18px rgba(0,0,0,.18);
    }
    button:hover:enabled { filter:brightness(1.08); border-color:rgba(255,255,255,.16); box-shadow:0 10px 22px rgba(0,0,0,.24); }
    button:active:enabled { transform:translateY(1px); }
    button:disabled { opacity:.33; cursor:not-allowed; box-shadow:none; }

    .primary { background:linear-gradient(180deg, #3b82f6 0%, #1d4ed8 100%); }
    .success { background:linear-gradient(180deg, #22c55e 0%, #15803d 100%); }
    .warnBtn { background:linear-gradient(180deg, #f59e0b 0%, #b45309 100%); }
    .standbyBtn { background:linear-gradient(180deg, #38bdf8 0%, #0369a1 100%); }
    .standbyBtn:enabled { box-shadow:0 0 0 2px rgba(56,189,248,.22), 0 12px 28px rgba(3,105,161,.30); }
    .abort { background:linear-gradient(180deg, #ef4444 0%, #b91c1c 100%) !important; }

    .telemetry {
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:6px;
    }

    .metric {
      position:relative;
      padding:7px;
      border-radius:13px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.025)),
        linear-gradient(135deg, rgba(255,255,255,.015), transparent 70%);
      border:1px solid rgba(255,255,255,.07);
      overflow:hidden;
    }
    .metric::after{
      content:"";
      position:absolute;
      inset:auto -20% -45% auto;
      width:70px; height:70px;
      background:radial-gradient(circle, var(--themeGlow), transparent 70%);
      pointer-events:none;
    }
    .metricLabel {
      font-size:10px;
      color:var(--muted);
      text-transform:uppercase;
      letter-spacing:.65px;
      margin-bottom:4px;
      font-weight:700;
    }
    .metricValue {
      font-size:15px;
      font-weight:900;
      letter-spacing:.1px;
    }
    .metricSmall { font-size:10px; color:var(--muted); margin-top:3px; }

    .pidControl {
      margin-top:8px;
      padding:8px;
      border-radius:14px;
      background:
        linear-gradient(180deg, rgba(96,165,250,.10), rgba(96,165,250,.035)),
        linear-gradient(135deg, rgba(255,255,255,.035), transparent 72%);
      border:1px solid rgba(96,165,250,.18);
    }
    .pidControlHeader {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:6px;
      margin-bottom:6px;
      font-size:11px;
      font-weight:900;
      letter-spacing:.2px;
    }
    .pidControlHeader small {
      color:var(--muted);
      font-size:9px;
      font-weight:800;
      text-transform:uppercase;
      letter-spacing:.55px;
    }
    .pidRow { display:grid; grid-template-columns:1fr auto; gap:6px; align-items:center; }
    .pidRow input {
      width:100%;
      min-width:0;
      padding:8px 9px;
      border-radius:11px;
      color:#f8fbff;
      background:rgba(4,12,24,.55);
      border:1px solid rgba(255,255,255,.12);
      outline:none;
      font-weight:800;
    }
    .pidRow input:focus { border-color:rgba(96,165,250,.55); box-shadow:0 0 0 3px rgba(96,165,250,.14); }
    .liveSvButton { background:linear-gradient(180deg, #60a5fa 0%, #2563eb 100%); white-space:nowrap; }
    .pidStatus {
      min-height:14px;
      margin-top:5px;
      color:#bfdbfe;
      font-size:10px;
      line-height:1.25;
    }

    .stagePanel {
      padding:8px;
      border-radius:13px;
      background:linear-gradient(180deg, rgba(96,165,250,.10), rgba(96,165,250,.04));
      border:1px solid rgba(96,165,250,.18);
      white-space:pre-line;
      line-height:1.32;
      font-size:12px;
    }

    .errorPanel {
      margin-top:6px;
      padding:7px;
      border-radius:13px;
      background:rgba(239,68,68,.08);
      border:1px solid rgba(239,68,68,.18);
      color:#fecaca;
      font-size:10px;
      min-height:14px;
    }

    .notes {
      color:var(--muted);
      font-size:11px;
      line-height:1.35;
    }

    .footerRow {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:8px;
      padding:0 10px 10px 10px;
      color:var(--muted);
      font-size:11px;
    }

    .subtlePill {
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:4px 8px;
      border-radius:999px;
      font-size:11px;
      font-weight:800;
      color:#dbeafe;
      background:rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.07);
    }
  </style>
</head>
<body class="theme-init">
  <div class="app">
    <div class="left card">
      <div class="header">
        <div class="titleWrap">
          <div class="title">COSCON IS — direct UDP control</div>
          <div class="subtitle">Native status and verified automated transitions; keep the COSCON web page closed</div>
        </div>
        <div class="statusCluster">
          <div id="pressureWarning" class="pressureWarning">Warning: Pressure too high</div>
          <div id="topStageBadge" class="statusBadge">INIT</div>
          <div id="timerBadge" class="timerBadge"><span>Remaining</span><strong>--:--</strong></div>
        </div>
      </div>
      <div class="cosconNative">
        <div class="cosconHero">
          <small>Current COSCON state</small>
          <strong id="cosconMode">UNKNOWN</strong>
          <small>Interlock: <span id="cosconInterlock">UNKNOWN</span></small>
        </div>
        <div class="cosconMetric"><span>Measured energy</span><strong id="cosconEnergy">nan V</strong></div>
        <div class="cosconMetric"><span>Measured emission</span><strong id="cosconEmission">nan mA</strong></div>
        <div class="cosconMetric"><span>Filament current</span><strong id="cosconFilament">nan A</strong></div>
        <div class="cosconMetric"><span>Target</span><strong id="cosconTarget">nan V / nan mA</strong></div>
        <div id="cosconDetails" class="cosconDetails">No COSCON telemetry yet.</div>
      </div>
    </div>

    <div class="right">
      <div class="card section">
        <div class="sectionTitle">
          <span>Run controls</span>
          <span class="accentDot"></span>
        </div>
        <div id="prompt" class="promptBox">Waiting for instructions…</div>
        <div class="buttonGrid">
          <button class="primary" data-token="yes">Yes</button>
          <button data-token="no">No</button>
          <button class="primary" style="grid-column:1/3" data-token="r">Start automated run</button>
          <button class="warnBtn" data-token="o">Argon valve opened</button>
          <button class="warnBtn" data-token="c">Argon valve closed</button>
          <button class="abort" style="grid-column:1/3" data-token="abort">Abort / COSCON safe stop</button>
        </div>
      </div>

      <div class="card section">
        <div class="sectionTitle">
          <span>Live status</span>
          <span class="subtlePill">Dynamic colors by stage</span>
        </div>
        <div id="stage" class="stagePanel">Stage: INIT</div>
        <div class="telemetry" style="margin-top:8px;">
          <div class="metric">
            <div class="metricLabel">Pressure</div>
            <div id="pressureVal" class="metricValue">nan</div>
            <div class="metricSmall">mbar</div>
          </div>
          <div class="metric">
            <div class="metricLabel">PID PV</div>
            <div id="pvVal" class="metricValue">nan</div>
            <div class="metricSmall">°C</div>
          </div>
          <div class="metric">
            <div class="metricLabel">PID SV</div>
            <div id="svVal" class="metricValue">nan</div>
            <div class="metricSmall">°C</div>
          </div>
          <div class="metric">
            <div class="metricLabel">Keysight</div>
            <div id="viVal" class="metricValue">nan / nan</div>
            <div class="metricSmall">V / A</div>
          </div>
        </div>
        <div class="pidControl">
          <div class="pidControlHeader">
            <span>Live PID SV control</span>
            <small>S1 remote write</small>
          </div>
          <div class="pidRow">
            <input id="pidSvInput" type="number" step="1" inputmode="decimal" placeholder="New SV, e.g. 620">
            <button id="setSvBtn" class="liveSvButton" type="button">Set PID SV</button>
          </div>
          <div id="pidCommandStatus" class="pidStatus">You can change the oven set value while the run is active.</div>
        </div>
        <div id="lastError" class="errorPanel"></div>
      </div>

      <div class="card section">
        <div class="sectionTitle">
          <span>Quick summary</span>
          <span class="subtlePill">Abort → SV 20 °C</span>
        </div>
        <div class="notes">
          COSCON Degas, Operate and Standby transitions are controlled and verified automatically. The argon leak valve remains manual. The system will not activate high voltage until pressure is stable and all readbacks are safe.
        </div>
      </div>

      <div class="footerRow">
        <div>Keep an operator present. Do not open the COSCON web interface while this phase is active.</div>
        <div id="updatedAt">No telemetry yet</div>
      </div>
    </div>
  </div>

<script>
  const buttons = Array.from(document.querySelectorAll('button[data-token]'));
  const promptEl = document.getElementById('prompt');
  const stageEl = document.getElementById('stage');
  const badgeEl = document.getElementById('topStageBadge');
  const timerBadgeEl = document.getElementById('timerBadge');
  const timerValueEl = timerBadgeEl ? timerBadgeEl.querySelector('strong') : null;
  const timerLabelEl = timerBadgeEl ? timerBadgeEl.querySelector('span') : null;
  const pressureVal = document.getElementById('pressureVal');
  const pressureWarningEl = document.getElementById('pressureWarning');
  const pvVal = document.getElementById('pvVal');
  const svVal = document.getElementById('svVal');
  const viVal = document.getElementById('viVal');
  const lastErrorEl = document.getElementById('lastError');
  const updatedAtEl = document.getElementById('updatedAt');
  const pidSvInput = document.getElementById('pidSvInput');
  const setSvBtn = document.getElementById('setSvBtn');
  const pidCommandStatus = document.getElementById('pidCommandStatus');
  const cosconMode = document.getElementById('cosconMode');
  const cosconInterlock = document.getElementById('cosconInterlock');
  const cosconEnergy = document.getElementById('cosconEnergy');
  const cosconEmission = document.getElementById('cosconEmission');
  const cosconFilament = document.getElementById('cosconFilament');
  const cosconTarget = document.getElementById('cosconTarget');
  const cosconDetails = document.getElementById('cosconDetails');

  function setAllowed(tokens) {
    const allowed = new Set(tokens || []);
    allowed.add('abort');
    for (const btn of buttons) {
      const tok = btn.getAttribute('data-token');
      btn.disabled = !allowed.has(tok);
    }
  }

  function setPrompt(data) {
    promptEl.textContent = data.message || 'Waiting for instructions…';
    setAllowed(data.allowed || []);
  }

  function fmt(value, fallback='nan') {
    if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return fallback;
    return String(value);
  }

  function themeForStage(stageText) {
    const t = (stageText || '').toUpperCase();
    if (t.includes('ABORT')) return 'theme-abort';
    if (t.includes('DONE')) return 'theme-done';
    if (t.includes('DEGASS')) return 'theme-degassing';
    if (t.includes('SPUTTER')) return 'theme-sputter';
    if (t.includes('STANDBY')) return 'theme-standby';
    if (t.includes('ANNEAL')) return 'theme-anneal';
    return 'theme-init';
  }

  function setStage(text) {
    stageEl.textContent = text || 'Stage: INIT';
    const m = /^Stage:\s*(.+)$/m.exec(text || '');
    const label = m ? m[1] : 'INIT';
    badgeEl.textContent = label;
    document.body.className = themeForStage(label);
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || seconds === '' || Number.isNaN(Number(seconds))) return '--:--';
    const s = Math.max(0, Math.round(Number(seconds)));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    return `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  }

  function updateTimer(snap) {
    if (!timerValueEl || !timerLabelEl) return;
    const label = snap.phase_timer_label || 'Remaining';
    timerLabelEl.textContent = label.length > 18 ? label.slice(0, 18) : label;
    timerValueEl.textContent = formatDuration(snap.phase_remaining_s);
    if (snap.phase_remaining_s === null || snap.phase_remaining_s === undefined) {
      timerBadgeEl.style.opacity = '0.72';
    } else {
      timerBadgeEl.style.opacity = '1';
    }
  }

  function setSnapshot(snap) {
    pressureVal.textContent = fmt(snap.pressure_mbar);
    if (pressureWarningEl) {
      const pressureNumber = Number(snap.pressure_mbar);
      const warningThreshold = Number(snap.pressure_warning_mbar);
      const limit = (!Number.isNaN(warningThreshold) && warningThreshold > 0) ? warningThreshold : 5e-5;
      pressureWarningEl.style.display = (!Number.isNaN(pressureNumber) && pressureNumber > limit) ? 'block' : 'none';
    }
    pvVal.textContent = fmt(snap.oven_pv_c);
    svVal.textContent = fmt(snap.oven_sv_c);
    viVal.textContent = `${fmt(snap.keysight_voltage_v)} / ${fmt(snap.keysight_current_a)}`;
    if (cosconMode) cosconMode.textContent = fmt(snap.coscon_mode, 'UNKNOWN');
    if (cosconInterlock) cosconInterlock.textContent = fmt(snap.coscon_interlock, 'UNKNOWN');
    if (cosconEnergy) cosconEnergy.textContent = `${fmt(snap.coscon_energy_v)} V`;
    if (cosconEmission) {
      const emission = Number(snap.coscon_emission_a);
      cosconEmission.textContent = Number.isNaN(emission) ? 'nan mA' : `${(emission * 1000).toFixed(3)} mA`;
    }
    if (cosconFilament) cosconFilament.textContent = `${fmt(snap.coscon_filament_a)} A`;
    if (cosconTarget) {
      const targetEmission = Number(snap.coscon_target_emission_a);
      const targetEmissionText = Number.isNaN(targetEmission) ? 'nan' : (targetEmission * 1000).toFixed(3);
      cosconTarget.textContent = `${fmt(snap.coscon_target_energy_v)} V / ${targetEmissionText} mA`;
    }
    if (cosconDetails) cosconDetails.textContent = snap.coscon_details || 'No active COSCON details.';
    updateTimer(snap);
    lastErrorEl.textContent = snap.last_error ? `Last error: ${snap.last_error}` : 'No active errors';
    updatedAtEl.textContent = snap.last_update ? `Updated: ${snap.last_update}` : 'No telemetry yet';
  }

  buttons.forEach(btn => {
    btn.addEventListener('click', async () => {
      const token = btn.getAttribute('data-token');
      try {
        await window.pywebview.api.send_token(token);
        btn.blur();
        if (token !== 'abort') {
          promptEl.textContent = 'Waiting for next step…';
          setAllowed([]);
        }
      } catch (err) {
        console.error(err);
      }
    });
  });

  if (setSvBtn) {
    setSvBtn.addEventListener('click', async () => {
      const raw = (pidSvInput.value || '').trim();
      const value = Number(raw.replace(',', '.'));
      if (raw === '' || Number.isNaN(value)) {
        pidCommandStatus.textContent = 'Please enter a valid PID SV value first.';
        return;
      }
      pidCommandStatus.textContent = `PID SV request sent: ${value} °C`;
      try {
        await window.pywebview.api.send_token(`pid_sv:${value}`);
      } catch (err) {
        pidCommandStatus.textContent = `Could not send PID SV request: ${err}`;
        console.error(err);
      }
    });
  }

  async function pump() {
    try {
      const msgs = await window.pywebview.api.pull_messages();
      for (const msg of msgs) {
        if (msg.kind === 'prompt') setPrompt(msg.payload);
        if (msg.kind === 'stage') setStage(msg.payload);
        if (msg.kind === 'snapshot') setSnapshot(msg.payload);
        if (msg.kind === 'pid_status' && pidCommandStatus) pidCommandStatus.textContent = msg.payload;
        if (msg.kind === 'close') window.close();
      }
    } catch (err) {
      console.error(err);
    }
    setTimeout(pump, 220);
  }

  setAllowed([]);
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
    html = HTML_TEMPLATE
    api = UnifiedUIApi(command_q, event_q)
    webview.create_window(title, html=html, js_api=api, width=width, height=height, confirm_close=True)
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
        self.state_lock = threading.RLock()
        self.stop_event = mp.Event()
        self.aborted = False
        self.coscon_activation_requested = False
        self.coscon_safe_state_confirmed = False
        self.background_fault_lock = threading.RLock()
        self.background_fault: Optional[str] = None
        self.state.coscon_target_energy_v = self.cfg.coscon_energy_target_v
        self.state.coscon_target_emission_a = self.cfg.coscon_emission_target_a

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
        self.coscon = COSCONUDPClient(
            ip=self.cfg.coscon_ip,
            port=self.cfg.coscon_udp_port,
            timeout_s=self.cfg.coscon_udp_timeout_s,
        )

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

    def _set_stage(self, stage: str, cycle: Optional[int] = None) -> None:
        self.state.stage = stage
        self.state.phase_remaining_s = None
        self.state.phase_total_s = None
        self.state.phase_timer_label = "Remaining"
        if cycle is not None:
            self.state.cycle = cycle
        self._update_ui_status()

    def _update_ui_status(self) -> None:
        if self.ui.is_running():
            snap = self.state.snapshot()
            snap["pressure_warning_mbar"] = self.cfg.pressure_warning_mbar
            timer_label = snap.get("phase_timer_label") or "Remaining"
            timer_text = self._format_remaining(snap.get("phase_remaining_s"))
            self.ui.set_stage(
                f"Stage: {snap['stage']}\n"
                f"Cycle: {snap['cycle']}\n"
                f"{timer_label}: {timer_text}\n"
                f"PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C | P={fmt_opt(snap['pressure_mbar'], '.2e')} mbar\n"
                f"COSCON={snap['coscon_mode']} | Interlock={snap['coscon_interlock']} | E={fmt_opt(snap['coscon_energy_v'], '.1f')} V | Iem={fmt_opt(snap['coscon_emission_a'], '.4e')} A"
            )
            self.ui.set_snapshot(snap)

    def _poll_ui_background(self) -> None:
        if self.ui.is_running():
            self.ui.poll_background_tokens()
        with self.background_fault_lock:
            fault = self.background_fault
        if fault:
            raise RuntimeError(f"Background safety monitor: {fault}")

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
            try:
                pressure = self.xgs600.read_pressure_mbar()
            except Exception as exc:
                pressure = None
                self.state.last_error = f"Pressure read failed: {exc}"

            try:
                oven_pv = self.pid.read_process_value_c()
            except Exception as exc:
                oven_pv = None
                self.state.last_error = f"PID PV read failed: {exc}"

            try:
                oven_sv = self.pid.read_setpoint_c()
            except Exception as exc:
                oven_sv = None
                self.state.last_error = f"PID SV read failed: {exc}"

            voltage = None
            current = None
            if self.keysight is not None:
                try:
                    voltage, current = self.keysight.read_voltage_current()
                except Exception as exc:
                    self.state.last_error = f"Keysight read failed: {exc}"

            coscon_status = None
            coscon_monitor = None
            try:
                coscon_status = self.coscon.get_status()
                coscon_monitor = self.coscon.get_monitor_values()
                if not coscon_status.interlock_ok:
                    with self.background_fault_lock:
                        self.background_fault = (
                            f"COSCON interlock={coscon_status.interlock}: "
                            f"{coscon_status.details}"
                        )
                elif coscon_status.mode_key == "error":
                    with self.background_fault_lock:
                        self.background_fault = f"COSCON Mode=Error: {coscon_status.details}"
                elif self.state.stage.startswith("ANNEAL") and coscon_status.mode_key not in {"standby", "off"}:
                    with self.background_fault_lock:
                        self.background_fault = (
                            f"Unexpected COSCON mode {coscon_status.mode} during annealing"
                        )
            except Exception as exc:
                self.state.last_error = f"COSCON monitor failed: {exc}"

            with self.state_lock:
                self.state.pressure_mbar = pressure
                self.state.oven_pv_c = oven_pv
                self.state.oven_sv_c = oven_sv
                self.state.keysight_voltage_v = voltage
                self.state.keysight_current_a = current
                if coscon_status is not None:
                    self._apply_coscon_status(coscon_status)
                if coscon_monitor is not None:
                    self._apply_coscon_monitor(coscon_monitor)
                self.state.last_update = datetime.now()

            snap = self.state.snapshot()
            self.logger.log_snapshot(snap)
            print(
                f"[{now_str()}] cycle={snap['cycle']} stage={snap['stage']} | "
                f"P={fmt_opt(snap['pressure_mbar'], '.2e')} mbar | "
                f"PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C | "
                f"V={fmt_opt(snap['keysight_voltage_v'], '.3f')} V | I={fmt_opt(snap['keysight_current_a'], '.4f')} A | "
                f"COSCON={snap['coscon_mode']} E={fmt_opt(snap['coscon_energy_v'], '.1f')} V Iem={fmt_opt(snap['coscon_emission_a'], '.4e')} A"
            )
            if pressure is not None and pressure == pressure and pressure > self.cfg.pressure_warning_mbar:
                warn(
                    f"Pressure is above warning threshold ({pressure:.2e} mbar > {self.cfg.pressure_warning_mbar:.2e} mbar)."
                )
            self._update_ui_status()
            time.sleep(self.cfg.monitor_period_s)

    def _apply_coscon_status(self, status: COSCONStatus) -> None:
        self.state.coscon_mode = status.mode
        self.state.coscon_interlock = status.interlock
        self.state.coscon_details = status.details

    def _apply_coscon_monitor(self, monitor: COSCONMonitorValues) -> None:
        self.state.coscon_energy_v = monitor.energy_v
        self.state.coscon_emission_a = monitor.emission_current_a
        self.state.coscon_filament_a = monitor.filament_current_a

    def _read_pressure_strict(self, context: str) -> float:
        try:
            pressure = self.xgs600.read_pressure_mbar(strict=True)
        except Exception as exc:
            raise RuntimeError(f"Pressure unavailable during {context}: {exc}") from exc
        if not math.isfinite(pressure) or pressure <= 0:
            raise RuntimeError(f"Unsafe pressure during {context}: {pressure!r}")
        with self.state_lock:
            self.state.pressure_mbar = pressure
            self.state.last_update = datetime.now()
        self._update_ui_status()
        self.logger.log_snapshot(self.state.snapshot(), note=f"Pressure check: {context}")
        return pressure

    def _read_coscon_snapshot(self, context: str) -> tuple[COSCONStatus, COSCONMonitorValues]:
        status = self.coscon.get_status()
        monitor = self.coscon.get_monitor_values()
        with self.state_lock:
            self._apply_coscon_status(status)
            self._apply_coscon_monitor(monitor)
            self.state.last_update = datetime.now()
        self._update_ui_status()
        self.logger.log_snapshot(self.state.snapshot(), note=f"COSCON snapshot: {context}")
        if not status.interlock_ok:
            raise RuntimeError(
                f"COSCON interlock is not OK during {context}: "
                f"{status.interlock} ({status.details})"
            )
        if status.mode_key == "error":
            self._log_coscon_diagnostics(f"MODE ERROR during {context}")
            raise RuntimeError(f"COSCON Mode=Error during {context}: {status.details}")
        return status, monitor

    def _log_coscon_diagnostics(self, label: str) -> None:
        warn(f"COSCON diagnostic snapshot: {label}")
        for description, getter in (
            ("status", self.coscon.get_status),
            ("targets", self.coscon.get_target_values),
            ("monitor", self.coscon.get_monitor_values),
            ("diagnostics", self.coscon.get_diagnostic_values),
        ):
            try:
                info(f"COSCON {description}: {getter()}")
            except Exception as exc:
                warn(f"COSCON {description} unavailable: {exc}")

    def _wait_for_coscon_modes(
        self,
        modes: set[str],
        timeout_s: float,
        context: str,
        *,
        allow_bad_interlock: bool = False,
    ) -> COSCONStatus:
        wanted = {mode.lower() for mode in modes}
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            if allow_bad_interlock:
                if self.ui.is_running():
                    try:
                        self.ui.poll_background_tokens()
                    except KeyboardInterrupt:
                        pass
            else:
                self._poll_ui_background()
            status = self.coscon.get_status()
            last = status
            with self.state_lock:
                self._apply_coscon_status(status)
                self.state.last_update = datetime.now()
            self._update_ui_status()
            if not allow_bad_interlock and not status.interlock_ok:
                raise RuntimeError(
                    f"COSCON interlock changed during {context}: "
                    f"{status.interlock} ({status.details})"
                )
            if status.mode_key == "error":
                self._log_coscon_diagnostics(f"Mode=Error while waiting during {context}")
                raise RuntimeError(f"COSCON error during {context}: {status.details}")
            if status.mode_key in wanted:
                return status
            time.sleep(self.cfg.coscon_poll_s)
        raise RuntimeError(
            f"Timeout during {context}; expected {sorted(modes)}, "
            f"last status={last.raw if last else 'unavailable'}"
        )

    def _best_effort_coscon_safe_stop(self, reason: str) -> None:
        warn(f"COSCON safe-stop path: {reason}")
        try:
            status = self.coscon.get_status()
            with self.state_lock:
                self._apply_coscon_status(status)
            if status.mode_key == "standby":
                self.coscon_safe_state_confirmed = True
                return
            if status.mode_key == "off":
                self.coscon_safe_state_confirmed = True
                return
        except Exception as exc:
            status = None
            warn(f"Could not read COSCON state before safe stop: {exc}")

        # Active Degassing rejects explicit Standby on this firmware; request Off.
        if status is not None and status.mode_key in {"degas", "degassing"}:
            try:
                self.coscon.switch_to_off()
            except Exception as exc:
                warn(f"SwitchToOff reply problem during Degas safe stop: {exc}")
            try:
                self._wait_for_coscon_modes(
                    {"Off"}, self.cfg.coscon_off_timeout_s, "Degas emergency Off",
                    allow_bad_interlock=True,
                )
                self.coscon_safe_state_confirmed = True
            except Exception as exc:
                warn(f"CRITICAL: COSCON Off could not be confirmed: {exc}")
            return

        try:
            self.coscon.switch_to_standby()
        except Exception as exc:
            warn(f"SwitchToStandby reply problem: {exc}")
        try:
            reached = self._wait_for_coscon_modes(
                {"Standby", "Off"},
                self.cfg.coscon_standby_timeout_s,
                "safe-stop Standby",
                allow_bad_interlock=True,
            )
            self.coscon_safe_state_confirmed = True
            if reached.mode_key in {"standby", "off"}:
                return
        except Exception as exc:
            warn(f"Standby/Off could not be confirmed: {exc}")

        try:
            self.coscon.switch_to_off()
        except Exception as exc:
            warn(f"SwitchToOff reply problem: {exc}")
        try:
            self._wait_for_coscon_modes(
                {"Off"}, self.cfg.coscon_off_timeout_s, "safe-stop Off",
                allow_bad_interlock=True,
            )
            self.coscon_safe_state_confirmed = True
        except Exception as exc:
            warn(
                "CRITICAL: automatic COSCON safe state could not be confirmed. "
                f"Use the local controls immediately. Details: {exc}"
            )

    def preflight(self) -> None:
        banner("SPUTTER-ANNEAL PRE-FLIGHT")
        if not self._ask_yes_no("Have you connected and checked the sputter-gun cable?"):
            raise RuntimeError("Run cancelled: sputter-gun cable not confirmed.")
        if not self._ask_yes_no("Have you switched on the sputtering electronics?"):
            raise RuntimeError("Run cancelled: sputtering electronics not confirmed.")
        if not self._ask_yes_no("Are the COSCON web interface and Prodigy/SpecsLab closed?"):
            raise RuntimeError("Run cancelled: another COSCON control client may still be open.")
        if not self._ask_yes_no("Is a trained operator present with access to the local controls?"):
            raise RuntimeError("Run cancelled: local supervision not confirmed.")

        info(f"COSCON connection: {self.coscon.info()}")
        status, _monitor = self._read_coscon_snapshot("preflight")
        if status.mode_key not in {"off", "standby"}:
            raise RuntimeError(
                f"COSCON must start in Off or Standby, not {status.mode} ({status.details})."
            )
        self._wait_for_token("r", "Preflight passed. Click Start automated run.")

    def start_ui(self) -> None:
        banner("OPENING PHASE 02 INTERFACE")
        ok = self.ui.start()
        if ok:
            info("Native Phase 02 interface started; COSCON web embedding is disabled.")
        else:
            raise RuntimeError(
                "Could not start the Phase 02 UI. Install pywebview and WebView2 Runtime."
            )

    def automatic_degassing_step(self, cycle: int) -> None:
        self._set_stage("AUTO_DEGASSING", cycle)
        banner(f"CYCLE {cycle} - AUTOMATIC COSCON DEGASSING")

        pressure = self._read_pressure_strict("Degas preflight")
        if pressure > self.cfg.degas_start_max_pressure_mbar:
            raise RuntimeError(
                f"Degas blocked: pressure {pressure:.3e} mbar exceeds "
                f"{self.cfg.degas_start_max_pressure_mbar:.3e} mbar."
            )

        status, _monitor = self._read_coscon_snapshot("Degas preflight")
        if status.mode_key == "standby":
            info("COSCON is in Standby; switching to Off before the one complete Degas cycle.")
            try:
                self.coscon.switch_to_off()
            except COSCONCommunicationError as exc:
                warn(f"SwitchToOff reply missing; verifying state without resending: {exc}")
            self._wait_for_coscon_modes(
                {"Off"}, self.cfg.coscon_off_timeout_s, "Standby to Off before Degas"
            )
        elif status.mode_key != "off":
            raise RuntimeError(f"Degas requires Off; current mode is {status.mode}.")

        info("Requesting SwitchToDegas once.")
        try:
            self.coscon.switch_to_degas()
        except COSCONCommunicationError as exc:
            warn(f"SwitchToDegas reply missing; verifying state without resending: {exc}")

        self._wait_for_coscon_modes(
            {"Degas", "Degassing"}, 20.0, "entering Degas"
        )

        total_s = int(self.cfg.coscon_degas_timeout_minutes * 60)
        deadline = time.monotonic() + total_s
        self._set_phase_timer(total_s, total_s, "Degas timeout")
        while time.monotonic() < deadline:
            self._poll_ui_background()
            status, monitor = self._read_coscon_snapshot("automatic Degas")
            pressure = self._read_pressure_strict("automatic Degas")
            remaining = max(0, int(deadline - time.monotonic()))
            self._set_phase_timer(remaining, total_s, "Degas timeout")

            if pressure >= self.cfg.degas_abort_pressure_mbar:
                self._log_coscon_diagnostics("Degas pressure emergency")
                try:
                    self.coscon.switch_to_off()
                except Exception as exc:
                    warn(f"Emergency SwitchToOff reply problem: {exc}")
                self._wait_for_coscon_modes(
                    {"Off"}, self.cfg.coscon_off_timeout_s, "Degas pressure emergency Off",
                    allow_bad_interlock=True,
                )
                self.coscon_safe_state_confirmed = True
                raise RuntimeError(
                    f"Degas aborted: pressure reached {pressure:.3e} mbar "
                    f"(limit {self.cfg.degas_abort_pressure_mbar:.3e} mbar)."
                )

            if status.mode_key in {"standby", "off"}:
                if status.mode_key != "standby":
                    raise RuntimeError(
                        "Degas ended in Off. This integrated workflow requires the "
                        "validated natural Standby result before sputtering."
                    )
                self.coscon_safe_state_confirmed = True
                self._set_phase_timer(0, total_s, "Degas timeout")
                info(
                    f"Complete Degas finished naturally in Standby. "
                    f"Pressure={pressure:.3e} mbar, filament={monitor.filament_current_a:.3f} A."
                )
                return

            if status.mode_key not in {"degas", "degassing"}:
                raise RuntimeError(
                    f"Unexpected COSCON mode during Degas: {status.mode} ({status.details})."
                )
            time.sleep(self.cfg.coscon_poll_s)

        self._log_coscon_diagnostics("Degas timeout")
        self._best_effort_coscon_safe_stop("Degas timeout")
        raise RuntimeError("Automatic Degas did not finish before its configured timeout.")

    def prompt_open_valve(self, cycle: int) -> None:
        self._set_stage("OPEN_ARGON_VALVE", cycle)
        banner(f"CYCLE {cycle} - OPEN ARGON LEAK VALVE")
        self._wait_for_token(
            "o",
            "Open the manual argon leak valve slowly. When pressure is near "
            f"{self.cfg.target_ar_pressure_mbar:.2e} mbar, click Argon valve opened. "
            "The script will then verify pressure stability before applying high voltage.",
        )

    def wait_for_stable_argon_pressure(self, cycle: int) -> None:
        self._set_stage("ARGON_PRESSURE_STABILIZATION", cycle)
        banner(f"CYCLE {cycle} - VERIFYING ARGON PRESSURE")
        required_s = float(self.cfg.pressure_stable_seconds)
        stable_since = None
        hard_deadline = time.monotonic() + max(300.0, required_s * 5.0)
        center = self.cfg.target_ar_pressure_mbar
        relative = self.cfg.pressure_stable_relative_band
        lower_stability = center * (1.0 - relative)
        upper_stability = center * (1.0 + relative)

        while time.monotonic() < hard_deadline:
            self._poll_ui_background()
            status, _monitor = self._read_coscon_snapshot("argon stabilization")
            if status.mode_key != "standby":
                raise RuntimeError(
                    f"COSCON must remain in Standby while argon is stabilized; got {status.mode}."
                )
            pressure = self._read_pressure_strict("argon stabilization")

            in_safety_window = self.cfg.pressure_min_mbar <= pressure <= self.cfg.pressure_warning_mbar
            in_stability_band = lower_stability <= pressure <= upper_stability
            if not in_safety_window:
                stable_since = None
                warn(
                    f"Pressure {pressure:.3e} mbar is outside the allowed sputtering window "
                    f"[{self.cfg.pressure_min_mbar:.3e}, {self.cfg.pressure_warning_mbar:.3e}]."
                )
            elif in_stability_band:
                if stable_since is None:
                    stable_since = time.monotonic()
                stable_elapsed = time.monotonic() - stable_since
                remaining = max(0, int(required_s - stable_elapsed))
                self._set_phase_timer(remaining, int(required_s), "Pressure stable")
                if stable_elapsed >= required_s:
                    info(
                        f"Argon pressure stable for {required_s:.0f} s at "
                        f"{pressure:.3e} mbar."
                    )
                    return
            else:
                stable_since = None
                self._set_phase_timer(int(required_s), int(required_s), "Pressure stable")
                warn(
                    f"Pressure is safe but not yet in the target stability band "
                    f"[{lower_stability:.3e}, {upper_stability:.3e}] mbar: {pressure:.3e}."
                )
            time.sleep(self.cfg.coscon_poll_s)

        raise RuntimeError("Argon pressure did not become stable within five minutes.")

    def automatic_start_sputtering(self, cycle: int) -> None:
        self._set_stage("AUTO_SWITCH_TO_OPERATE", cycle)
        banner(f"CYCLE {cycle} - AUTOMATIC COSCON OPERATE")

        status, _monitor = self._read_coscon_snapshot("Operate preflight")
        if status.mode_key != "standby":
            raise RuntimeError(f"Operate requires Standby; current mode is {status.mode}.")
        pressure = self._read_pressure_strict("Operate preflight")
        if not (self.cfg.pressure_min_mbar <= pressure <= self.cfg.pressure_warning_mbar):
            raise RuntimeError(
                f"Operate blocked: pressure {pressure:.3e} mbar is outside "
                f"[{self.cfg.pressure_min_mbar:.3e}, {self.cfg.pressure_warning_mbar:.3e}]."
            )

        reply = self.coscon.validate_operate_target(
            self.cfg.coscon_emission_target_a,
            self.cfg.coscon_energy_target_v,
        )
        if "OK" not in reply.upper():
            raise RuntimeError(f"Unexpected ValidateOperateTarget reply: {reply}")

        self.coscon_activation_requested = True
        self.coscon_safe_state_confirmed = False
        info("Sending SwitchToOperate once; no blind retry will be performed.")
        try:
            self.coscon.switch_to_operate(
                self.cfg.coscon_emission_target_a,
                self.cfg.coscon_energy_target_v,
            )
        except COSCONCommunicationError as exc:
            warn(f"SwitchToOperate reply missing; polling state without resending: {exc}")

        deadline = time.monotonic() + self.cfg.coscon_operate_timeout_s
        while time.monotonic() < deadline:
            self._poll_ui_background()
            status, monitor = self._read_coscon_snapshot("SwitchToOperate transition")
            pressure = self._read_pressure_strict("SwitchToOperate transition")
            if pressure >= self.cfg.degas_abort_pressure_mbar:
                raise RuntimeError(
                    f"Pressure emergency during activation: {pressure:.3e} mbar."
                )
            if status.mode_key == "operating":
                break
            if status.mode_key not in {"standby", "switchingtooperate"}:
                raise RuntimeError(
                    f"Unexpected mode during activation: {status.mode} ({status.details})."
                )
            time.sleep(self.cfg.coscon_poll_s)
        else:
            self._log_coscon_diagnostics("Operate transition timeout")
            raise RuntimeError("COSCON did not reach Mode=Operating before timeout.")

        stable_hits = 0
        stable_deadline = time.monotonic() + 20.0
        while time.monotonic() < stable_deadline:
            status, monitor = self._read_coscon_snapshot("stable output verification")
            pressure = self._read_pressure_strict("stable output verification")
            if status.mode_key != "operating":
                raise RuntimeError(
                    f"COSCON left Operating during output verification: {status.mode}."
                )
            energy_ok = abs(monitor.energy_v - self.cfg.coscon_energy_target_v) <= self.cfg.coscon_energy_tolerance_v
            emission_ok = abs(monitor.emission_current_a - self.cfg.coscon_emission_target_a) <= self.cfg.coscon_emission_tolerance_a
            pressure_ok = self.cfg.pressure_min_mbar <= pressure <= self.cfg.pressure_warning_mbar
            stable_hits = stable_hits + 1 if energy_ok and emission_ok and pressure_ok else 0
            info(
                f"COSCON verify {stable_hits}/{self.cfg.coscon_stable_samples}: "
                f"E={monitor.energy_v:.2f} V, Iem={monitor.emission_current_a*1000:.3f} mA, "
                f"P={pressure:.3e} mbar"
            )
            if stable_hits >= self.cfg.coscon_stable_samples:
                info("Stable COSCON output confirmed.")
                return
            time.sleep(self.cfg.coscon_poll_s)

        self._log_coscon_diagnostics("Stable output timeout")
        raise RuntimeError("Operating was reached but stable measured output was not confirmed.")

    def run_sputter_timer(self, cycle: int) -> None:
        self._set_stage("AUTOMATED_SPUTTERING", cycle)
        banner(f"CYCLE {cycle} - AUTOMATED SPUTTERING")
        total_s = int(self.cfg.sputter_minutes * 60)
        self._set_phase_timer(total_s, total_s, "Sputtering left")
        start = time.monotonic()
        consecutive_bad_energy = 0
        consecutive_bad_emission = 0
        consecutive_bad_pressure = 0

        while True:
            self._poll_ui_background()
            elapsed = int(time.monotonic() - start)
            remaining = total_s - elapsed
            self._set_phase_timer(remaining, total_s, "Sputtering left")
            if remaining <= 0:
                break

            status, monitor = self._read_coscon_snapshot("sputtering countdown")
            pressure = self._read_pressure_strict("sputtering countdown")
            if status.mode_key != "operating":
                raise RuntimeError(
                    f"COSCON left Operating during sputtering: {status.mode} ({status.details})."
                )
            if pressure >= self.cfg.degas_abort_pressure_mbar:
                raise RuntimeError(
                    f"Pressure emergency during sputtering: {pressure:.3e} mbar."
                )

            energy_ok = abs(monitor.energy_v - self.cfg.coscon_energy_target_v) <= self.cfg.coscon_energy_tolerance_v
            emission_ok = abs(monitor.emission_current_a - self.cfg.coscon_emission_target_a) <= self.cfg.coscon_emission_tolerance_a
            pressure_ok = self.cfg.pressure_min_mbar <= pressure <= self.cfg.pressure_warning_mbar
            consecutive_bad_energy = 0 if energy_ok else consecutive_bad_energy + 1
            consecutive_bad_emission = 0 if emission_ok else consecutive_bad_emission + 1
            consecutive_bad_pressure = 0 if pressure_ok else consecutive_bad_pressure + 1

            if monitor.energy_v < 0.80 * self.cfg.coscon_energy_target_v:
                self._log_coscon_diagnostics("Major energy collapse")
                raise RuntimeError(
                    f"COSCON energy collapsed to {monitor.energy_v:.1f} V during sputtering."
                )
            if consecutive_bad_energy >= 3:
                raise RuntimeError("COSCON energy remained outside tolerance for 3 checks.")
            if consecutive_bad_emission >= 3:
                raise RuntimeError("COSCON emission remained outside tolerance for 3 checks.")
            if consecutive_bad_pressure >= 3:
                raise RuntimeError("Pressure remained outside the sputtering window for 3 checks.")

            mins, secs = divmod(max(0, remaining), 60)
            print(
                f"Sputter countdown: {mins:02d}:{secs:02d} | "
                f"E={monitor.energy_v:.2f} V | Iem={monitor.emission_current_a*1000:.3f} mA | "
                f"P={pressure:.3e} mbar"
            )
            time.sleep(self.cfg.coscon_poll_s)

        self._set_phase_timer(0, total_s, "Sputtering left")
        info("Sputter time finished with all monitored values valid.")

    def automatic_switch_to_standby(self, cycle: int) -> None:
        self._set_stage("AUTO_STANDBY", cycle)
        banner(f"CYCLE {cycle} - AUTOMATIC COSCON STANDBY")
        try:
            self.coscon.switch_to_standby()
        except COSCONCommunicationError as exc:
            warn(f"SwitchToStandby reply missing; verifying state without resending: {exc}")
        reached = self._wait_for_coscon_modes(
            {"Standby", "Off"},
            self.cfg.coscon_standby_timeout_s,
            "Operating to Standby",
        )
        if reached.mode_key != "standby":
            raise RuntimeError(
                f"COSCON reached {reached.mode}, but Standby is required before annealing."
            )
        self.coscon_safe_state_confirmed = True
        info("COSCON Standby confirmed before valve closure and annealing.")

    def prompt_close_valve(self, cycle: int) -> None:
        self._set_stage("CLOSE_ARGON_VALVE", cycle)
        banner(f"CYCLE {cycle} - CLOSE ARGON LEAK VALVE")
        self._wait_for_token(
            "c",
            "COSCON Standby is confirmed. Close the manual argon leak valve fully, "
            "then click Argon valve closed.",
        )
        pressure = self._read_pressure_strict("after argon valve closure")
        info(f"Pressure immediately after valve-closure confirmation: {pressure:.3e} mbar")

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
            self.automatic_degassing_step(1)
            for cycle in range(1, self.cfg.cycles + 1):
                self.prompt_open_valve(cycle)
                self.wait_for_stable_argon_pressure(cycle)
                self.automatic_start_sputtering(cycle)
                self.run_sputter_timer(cycle)
                self.automatic_switch_to_standby(cycle)
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
        if self.aborted or (self.coscon_activation_requested and not self.coscon_safe_state_confirmed):
            self._best_effort_coscon_safe_stop("Phase 02 shutdown/abort")

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
    print("  COSCON web embedding: disabled")
    print(f"  Cycles: {cfg.cycles}")
    print(f"  Sputter time: {cfg.sputter_minutes} min")
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
