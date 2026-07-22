"""
Sputtering-Annealings Controller v10.1
===================================

What this script does
---------------------
This script helps run the sputtering-annealing workflow in a semi-automatic way.

Main features:
- Opens a single Python window with:
  - the COSCON IS interface on the left
  - workflow buttons and live status on the right
- Guides the operator through:
  - sputter gun cable / sputtering electronics preflight
  - one initial degassing confirmation before the first sputtering-annealing cycle
  - sputter preset confirmation
  - leak valve open / close confirmation
  - mandatory sputtering standby confirmation before annealing ramp
  - sputtering countdown
  - live PID SV changes from the interface while the run is active
  - annealing at the configured PID setpoint
- Reads live values from:
  - chamber pressure (XGS600)
  - oven PID process value (PV / M1)
  - oven PID set value (SV / S1)
  - Keysight voltage/current, if connected
- Writes the PID setpoint remotely through S1
- PID serial communication is protected by a lock to avoid monitor/write collisions
- On abort, it tries to set the PID set value to 20 °C

Important notes
---------------
- The leak valve is still manual.
- COSCON is shown inside the Python interface through an embedded web view.
- The script logs telemetry to a CSV file in the output folder.
- The output folder is intentionally short:
    "<run name> Sputtering-Annealing"
  If that folder already exists, a short numeric suffix is added.

Basic usage
-----------
1. Install dependencies:
       pip install pyserial colorama requests pywebview
2. Run:
       python sputtering_annealings_controller_v10_1.py
3. Enter a run name when asked.
4. Use the buttons in the interface to confirm each manual step.

This file contains the usage explanation directly inside the .py, so no external
README is required.
"""


from __future__ import annotations

import csv
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
from dataclasses import dataclass, field
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
        "pyserial is required. Install it with: pip install pyserial colorama requests pywebview"
    ) from exc

try:
    import requests
except ImportError:
    requests = None  # type: ignore

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
    expected_degassing_minutes: int = 20
    sputter_minutes: int = 20
    anneal_target_c: float = 620.0
    anneal_hold_minutes: int = 10
    anneal_reset_c: float = 0.0
    abort_reset_c: float = 20.0

    # COSCON
    coscon_url: str = "http://192.168.236.186/"
    coscon_probe_timeout_s: float = 8.0
    coscon_ui_mode: str = "embedded_only"  # embedded_only | browser | none
    coscon_window_title: str = "Sputtering-Annealings Controller v10.1"
    coscon_window_width: int = 1500
    coscon_window_height: int = 950

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

    iframe {
      flex:1;
      width:100%;
      border:0;
      background:#fff;
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
          <div class="title">COSCON IS</div>
          <div class="subtitle">Embedded in this interface</div>
        </div>
        <div class="statusCluster">
          <div id="pressureWarning" class="pressureWarning">Warning: Pressure too high</div>
          <div id="topStageBadge" class="statusBadge">INIT</div>
          <div id="timerBadge" class="timerBadge"><span>Remaining</span><strong>--:--</strong></div>
        </div>
      </div>
      <iframe id="cosconFrame" src="__COSCON_URL__"></iframe>
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
          <button class="primary" data-token="r">Start run</button>
          <button class="success" data-token="g">Degassing started</button>
          <button class="success" data-token="d">Degassing finished</button>
          <button class="primary" data-token="s">Sputter preset ready</button>
          <button class="warnBtn" data-token="o">Valve opened</button>
          <button class="warnBtn" data-token="c">Valve closed</button>
          <button class="standbyBtn" style="grid-column:1/3" data-token="standby">Standby clicked</button>
          <button class="abort" style="grid-column:1/3" data-token="abort">Abort</button>
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
          COSCON is on the left. Buttons guide the workflow. Leak valve stays manual. PID SV can also be changed live from this panel. Standby confirmation is required before each annealing ramp.
        </div>
      </div>

      <div class="footerRow">
        <div>Use buttons for confirmations. Keep an operator present.</div>
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


def _unified_ui_target(command_q: mp.Queue, event_q: mp.Queue, title: str, width: int, height: int, coscon_url: str) -> None:
    import webview
    html = HTML_TEMPLATE.replace("__COSCON_URL__", coscon_url)
    api = UnifiedUIApi(command_q, event_q)
    webview.create_window(title, html=html, js_api=api, width=width, height=height, confirm_close=True)
    webview.start(debug=False)


class UnifiedUIClient:
    def __init__(self, enabled: bool, title: str, width: int, height: int, coscon_url: str) -> None:
        self.enabled = enabled
        self.title = title
        self.width = width
        self.height = height
        self.coscon_url = coscon_url
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
            args=(self.command_q, self.event_q, self.title, self.width, self.height, self.coscon_url),
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

        self.ui = UnifiedUIClient(
            enabled=(self.cfg.coscon_ui_mode == "embedded_only"),
            title=self.cfg.coscon_window_title,
            width=self.cfg.coscon_window_width,
            height=self.cfg.coscon_window_height,
            coscon_url=self.cfg.coscon_url,
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
                f"PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C | P={fmt_opt(snap['pressure_mbar'], '.2e')} mbar"
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

            self.state.pressure_mbar = pressure
            self.state.oven_pv_c = oven_pv
            self.state.oven_sv_c = oven_sv
            self.state.keysight_voltage_v = voltage
            self.state.keysight_current_a = current
            self.state.last_update = datetime.now()

            snap = self.state.snapshot()
            self.logger.log_snapshot(snap)
            print(
                f"[{now_str()}] cycle={snap['cycle']} stage={snap['stage']} | "
                f"P={fmt_opt(snap['pressure_mbar'], '.2e')} mbar | "
                f"PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C | "
                f"V={fmt_opt(snap['keysight_voltage_v'], '.3f')} V | I={fmt_opt(snap['keysight_current_a'], '.4f')} A"
            )
            if pressure is not None and pressure == pressure and pressure > self.cfg.pressure_warning_mbar:
                warn(
                    f"Pressure is above warning threshold ({pressure:.2e} mbar > {self.cfg.pressure_warning_mbar:.2e} mbar)."
                )
            self._update_ui_status()
            time.sleep(self.cfg.monitor_period_s)

    def preflight(self) -> None:
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
            info("Unified embedded interface started.")
        else:
            raise RuntimeError(
                "Could not start the embedded unified UI. Install pywebview and WebView2 Runtime."
            )

    def coscon_degassing_step(self, cycle: int) -> None:
        self._set_stage("DEGASSING", cycle)
        banner(f"CYCLE {cycle} - DEGASSING")
        self._wait_for_token("g", "Click 'Degassing started' when you start degassing in COSCON.")

        total_s = int(self.cfg.expected_degassing_minutes * 60)
        timer_stop = threading.Event()
        timer_thread = threading.Thread(
            target=self._run_phase_countdown_until_stop,
            args=(total_s, "Degassing left", timer_stop),
            daemon=True,
        )
        timer_thread.start()
        try:
            self._wait_for_token("d", "Click 'Degassing finished' when the automatic degassing has finished.")
        finally:
            timer_stop.set()
            timer_thread.join(timeout=2)
            self._set_phase_timer(0, total_s, "Degassing left")

    def coscon_start_sputter_preset(self, cycle: int) -> None:
        self._set_stage("SPUTTER_PRESET", cycle)
        banner(f"CYCLE {cycle} - SPUTTER PRESET")
        self._wait_for_token("s", "Click 'Sputter preset ready' when the sputter preset is running and HV is ready.")

    def prompt_open_valve(self, cycle: int) -> None:
        self._set_stage("OPEN_VALVE", cycle)
        banner(f"CYCLE {cycle} - OPEN LEAK VALVE")
        self._wait_for_token("o", "Click 'Valve opened' when the leak valve is open and pressure is stable.")

    def run_sputter_timer(self, cycle: int) -> None:
        self._set_stage("SPUTTERING", cycle)
        banner(f"CYCLE {cycle} - SPUTTERING")
        total_s = int(self.cfg.sputter_minutes * 60)
        self._set_phase_timer(total_s, total_s, "Sputtering left")
        start = time.time()
        while True:
            self._poll_ui_background()
            elapsed = int(time.time() - start)
            remaining = total_s - elapsed
            self._set_phase_timer(remaining, total_s, "Sputtering left")
            if remaining <= 0:
                break
            mins, secs = divmod(remaining, 60)
            snap = self.state.snapshot()
            print(
                f"Sputter countdown: {mins:02d}:{secs:02d} remaining | "
                f"P={fmt_opt(snap['pressure_mbar'], '.2e')} mbar | PV={fmt_opt(snap['oven_pv_c'], '.1f')} °C | SV={fmt_opt(snap['oven_sv_c'], '.1f')} °C"
            )
            time.sleep(1)
        self._set_phase_timer(0, total_s, "Sputtering left")
        info("Sputter time finished.")

    def prompt_close_valve(self, cycle: int) -> None:
        self._set_stage("CLOSE_VALVE", cycle)
        banner(f"CYCLE {cycle} - CLOSE LEAK VALVE")
        self._wait_for_token("c", "Click 'Valve closed' when the leak valve is fully closed.")

    def prompt_standby_clicked(self, cycle: int) -> None:
        self._set_stage("STANDBY_CONFIRM", cycle)
        banner(f"CYCLE {cycle} - SPUTTERING STANDBY CONFIRMATION")
        self._wait_for_token(
            "standby",
            "Before the annealing ramp can start, switch the sputtering system to standby/OFF. Then click 'Standby clicked'.",
        )

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
                self.coscon_start_sputter_preset(cycle)
                self.prompt_open_valve(cycle)
                self.run_sputter_timer(cycle)
                self.prompt_close_valve(cycle)
                self.prompt_standby_clicked(cycle)
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
    print(f"  COSCON URL: {cfg.coscon_url}")
    print(f"  Unified embedded window: yes")
    print(f"  Cycles: {cfg.cycles}")
    print(f"  Sputter time: {cfg.sputter_minutes} min")
    print(f"  Anneal target: {cfg.anneal_target_c:.0f} °C")
    print(f"  Anneal hold: {cfg.anneal_hold_minutes} min")
    print(f"  Anneal reset after cycle: {cfg.anneal_reset_c:.0f} °C")
    print(f"  Abort reset: {cfg.abort_reset_c:.0f} °C")
    print(f"  PID port/address: {cfg.pid_port} / {cfg.pid_address}")
    print("  PID serial lock/retries: enabled")
    print("\nRequirements for the unified UI on Windows:")
    print("  pip install pyserial colorama requests pywebview")
    print("  and make sure Microsoft Edge WebView2 Runtime is installed")

    controller = SputterAnnealController(cfg)
    controller.run()


if __name__ == "__main__":
    main()
