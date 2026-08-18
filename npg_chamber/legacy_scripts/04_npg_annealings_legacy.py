from __future__ import annotations

import atexit
import csv
import json
import math
import os
import signal


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
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from npg_chamber.config.run_parameters import (
    apply_overrides_to_namespace,
    load_phase_overrides,
    load_pyrometer_settings,
    write_effective_parameters,
)
from npg_chamber.devices.pyrometer import ImpacIPE140, PyrometerProfile, PyrometerSerialConfig

RUN_AUTOMATION_OVERRIDES = load_phase_overrides("anneal")
PYROMETER_SETTINGS = load_pyrometer_settings()
PYROMETER_PROFILE = PyrometerProfile(**PYROMETER_SETTINGS)
PYROMETER_SERIAL_CONFIG = PyrometerSerialConfig(port="COM10", baudrate=38400, address="00")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.widgets import TextBox, Button

try:
    import serial
except ImportError as exc:
    raise SystemExit("pyserial is required. Install it with: pip install pyserial matplotlib colorama") from exc

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
# USER SETTINGS
# =============================================================================
RUN_NAME_PROMPT = "Enter the name for this NPG annealing run: "
OUTPUT_BASE_FOLDER = _resolve_phase_data_parent("NPG Annealing Data")

PID_PORT = "COM9"
PID_BAUDRATE = 9600
PID_ADDRESS = "00"

KEYSIGHT_PORT = "COM17"
KEYSIGHT_BAUDRATE = 9600
KEYSIGHT_RANGE = "LOW"      # 15 V / 7 A
KEYSIGHT_VOLTAGE_LIMIT_V = 2.40
KEYSIGHT_OCP_A = 0.685

CK1_ARDUINO_PORT = "COM3"
CK1_ARDUINO_BAUDRATE = 9600

INITIAL_WAIT_S = 5 * 60
INITIAL_WAIT_TARGET_C = 200.0

FIRST_STAGE_TARGET_C = 350.0
FIRST_STAGE_HOLD_S = 15 * 60

SECOND_STAGE_TARGET_C = 600.0
SECOND_STAGE_HOLD_S = 40 * 60
STAGE_REACHED_MARGIN_C = 2.0
STAGE_STABLE_DURATION_S = 30.0
PAUSE_HOLD_OUTSIDE_TEMPERATURE_BAND = True
OVEN_SIGNAL_STALE_TIMEOUT_S = 10.0
OVEN_SIGNAL_INITIAL_GRACE_S = 15.0

COOLDOWN_TARGET_C = 0.0
POST_COOLDOWN_WAIT_S = 10 * 60

KEYSIGHT_RAMPDOWN_STEP_A = 0.005
KEYSIGHT_RAMPDOWN_STEP_S = 15
FIRST_RAMPDOWN_STEP_DELAY_S = 10
KEYSIGHT_ZERO_THRESHOLD_A = 0.003

READ_INTERVAL_S = 2.0
GUI_REFRESH_S = 1.0
DATA_FLUSH_S = 5.0

STATUS_MESSAGE_CURRENT_ZERO = "SWITCH OFF EVAPORATOR CURRENT"
AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED = os.environ.get("NPG_CHAMBER_UNIFIED_LAUNCHER", "").strip() == "1"

# Apply validated launcher values before SharedState captures the recipe defaults.
# COM ports and hard Keysight protection values remain fixed and are not included
# in the editable parameter schema.
apply_overrides_to_namespace("anneal", globals(), RUN_AUTOMATION_OVERRIDES)


# =============================================================================
# HELPERS
# =============================================================================
def ask_nonempty_text(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Please enter a non-empty value.")


def ask_positive_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip().replace(",", ".")
        try:
            value = float(raw)
            if value > 0:
                return value
        except Exception:
            pass
        print("Please enter a positive number.")


def log_ts() -> tuple[datetime, str, str]:
    ts = datetime.now()
    return ts, ts.strftime("%Y-%m-%d %H:%M:%S"), f"{ts.microsecond // 10000:02d}"


def banner(message: str) -> None:
    _ts, formatted, dec = log_ts()
    print(f"\n{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.CYAN}{'=' * 88}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{message}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'=' * 88}{Style.RESET_ALL}\n")


def info(message: str, color: str = "") -> None:
    _ts, formatted, dec = log_ts()
    print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {color}{message}{Style.RESET_ALL}")


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
            info(f"Serial cleanup: closed {label}.", Fore.CYAN)
        except Exception as exc:
            info(f"Serial cleanup warning for {label}: {exc}", Fore.RED)


def sleep_interruptibly(total_s: float, stop_event: threading.Event, chunk_s: float = 0.2) -> bool:
    deadline = time.time() + max(0.0, total_s)
    while time.time() < deadline:
        if stop_event.is_set():
            return False
        remaining = deadline - time.time()
        time.sleep(min(chunk_s, remaining))
    return not stop_event.is_set()


# =============================================================================
# STATE
# =============================================================================
@dataclass
class SharedState:
    stop_event: threading.Event = field(default_factory=threading.Event)
    finished_event: threading.Event = field(default_factory=threading.Event)
    rampdown_finished_event: threading.Event = field(default_factory=threading.Event)
    evaporator_poweroff_confirmed_event: threading.Event = field(default_factory=threading.Event)
    data_lock: threading.Lock = field(default_factory=threading.Lock)
    pid_lock: threading.Lock = field(default_factory=threading.Lock)
    keysight_lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.Lock = field(default_factory=threading.Lock)

    phase: str = "STARTING"
    phase_started_at: float = field(default_factory=time.time)
    phase_deadline_at: Optional[float] = None
    last_message: str = "Initializing"
    gui_action_message: str = "Ready."
    current_zero_notice_shown: bool = False
    consecutive_missing_evaporator_reads: int = 0
    anneal_finished_normally: bool = False
    abort_requested: bool = False
    safe_shutdown_completed: bool = False
    keysight_output_off_at_zero: bool = False

    first_stage_target_c: float = FIRST_STAGE_TARGET_C
    second_stage_target_c: float = SECOND_STAGE_TARGET_C
    first_stage_hold_s: float = FIRST_STAGE_HOLD_S
    second_stage_hold_s: float = SECOND_STAGE_HOLD_S

    oven_setpoint_c: Optional[float] = None
    oven_temperature_times: list[datetime] = field(default_factory=list)
    oven_temperature_values: list[float] = field(default_factory=list)

    pyrometer_temperature_times: list[datetime] = field(default_factory=list)
    pyrometer_temperature_values: list[float] = field(default_factory=list)
    sample_temperature_estimates: list[float] = field(default_factory=list)
    pyrometer_status_values: list[str] = field(default_factory=list)
    pyrometer_status: str = "disabled" if not PYROMETER_PROFILE.enabled else "waiting"
    pyrometer_last_error: str = ""
    pyrometer_confirmed_emissivity_percent: Optional[float] = None
    temperature_view_mode: str = PYROMETER_PROFILE.default_view

    ck1_temperature_times: list[datetime] = field(default_factory=list)
    ck1_temperature_values: list[float] = field(default_factory=list)

    keysight_current_times: list[datetime] = field(default_factory=list)
    keysight_current_values: list[float] = field(default_factory=list)
    keysight_voltage_times: list[datetime] = field(default_factory=list)
    keysight_voltage_values: list[float] = field(default_factory=list)

    initial_keysight_current_a: Optional[float] = None
    last_keysight_set_current_a: Optional[float] = None
    final_keysight_current_a: Optional[float] = None

    def set_phase(self, phase: str, message: str = "", duration_s: Optional[float] = None) -> None:
        with self.state_lock:
            self.phase = phase
            self.phase_started_at = time.time()
            self.phase_deadline_at = (self.phase_started_at + max(0.0, duration_s)) if duration_s is not None else None
            if message:
                self.last_message = message

    def phase_elapsed_s(self) -> float:
        with self.state_lock:
            return time.time() - self.phase_started_at

    def phase_remaining_s(self) -> Optional[float]:
        with self.state_lock:
            if self.phase_deadline_at is None:
                return None
            return max(0.0, self.phase_deadline_at - time.time())

    def set_message(self, message: str) -> None:
        with self.state_lock:
            self.last_message = message

    def set_gui_action_message(self, message: str) -> None:
        with self.state_lock:
            self.gui_action_message = message
            self.last_message = message

    def mark_abort(self, reason: str = "Run aborted by user.") -> None:
        with self.state_lock:
            self.abort_requested = True
            self.last_message = reason
            self.gui_action_message = reason

    def set_first_stage_target_c(self, target_c: float) -> None:
        with self.state_lock:
            self.first_stage_target_c = float(target_c)

    def get_first_stage_target_c(self) -> float:
        with self.state_lock:
            return float(self.first_stage_target_c)

    def set_second_stage_target_c(self, target_c: float) -> None:
        with self.state_lock:
            self.second_stage_target_c = float(target_c)

    def get_second_stage_target_c(self) -> float:
        with self.state_lock:
            return float(self.second_stage_target_c)

    def set_first_stage_hold_s(self, hold_s: float) -> None:
        with self.state_lock:
            self.first_stage_hold_s = max(0.0, float(hold_s))
            if self.phase == "HOLD_FIRST":
                self.phase_deadline_at = self.phase_started_at + self.first_stage_hold_s

    def get_first_stage_hold_s(self) -> float:
        with self.state_lock:
            return float(self.first_stage_hold_s)

    def set_second_stage_hold_s(self, hold_s: float) -> None:
        with self.state_lock:
            self.second_stage_hold_s = max(0.0, float(hold_s))
            if self.phase == "HOLD_SECOND":
                self.phase_deadline_at = self.phase_started_at + self.second_stage_hold_s

    def get_second_stage_hold_s(self) -> float:
        with self.state_lock:
            return float(self.second_stage_hold_s)

    def show_switch_off_prompt(self) -> None:
        with self.state_lock:
            self.current_zero_notice_shown = True
            self.consecutive_missing_evaporator_reads = 0
            self.last_message = STATUS_MESSAGE_CURRENT_ZERO
            self.evaporator_poweroff_confirmed_event.clear()

    def clear_switch_off_prompt(self, message: str = "Evaporator switched off detected.") -> None:
        with self.state_lock:
            self.current_zero_notice_shown = False
            self.consecutive_missing_evaporator_reads = 0
            self.last_message = message
            self.evaporator_poweroff_confirmed_event.set()


# =============================================================================
# PID COMMUNICATION (reusing the same approach as your other scripts)
# =============================================================================
PID_EOT = b"\x04"
PID_ENQ = b"\x05"
PID_STX = b"\x02"
PID_ETX = b"\x03"
PID_ACK = b"\x06"
PID_NAK = b"\x15"


def pid_xor_bcc(identifier_plus_data_plus_etx: bytes) -> bytes:
    value = 0
    for b in identifier_plus_data_plus_etx:
        value ^= b
    return bytes([value])


def pid_parse_frame(raw: bytes) -> dict:
    if raw == b"":
        return {"status": "NO_RESPONSE", "raw": raw}
    if raw == PID_ACK:
        return {"status": "ACK", "raw": raw}
    if raw == PID_NAK:
        return {"status": "NAK", "raw": raw}
    if raw == PID_EOT:
        return {"status": "EOT", "raw": raw}
    if len(raw) >= 5 and raw[0:1] == PID_STX:
        try:
            etx_index = raw.index(PID_ETX)
        except ValueError:
            return {"status": "UNKNOWN_FRAME", "raw": raw, "decoded": raw.decode(errors="ignore")}

        core = raw[1:etx_index]
        if len(core) < 2:
            return {"status": "SHORT_FRAME", "raw": raw, "decoded": raw.decode(errors="ignore")}

        ident = core[:2].decode(errors="ignore")
        data_text = core[2:].decode(errors="ignore")
        return {
            "status": "DATA",
            "raw": raw,
            "decoded": raw.decode(errors="ignore"),
            "ident": ident,
            "data": data_text,
        }
    return {"status": "UNKNOWN", "raw": raw, "decoded": raw.decode(errors="ignore")}


def pid_parse_numeric_ascii(data_text: str) -> Optional[float]:
    stripped = data_text.strip()
    if stripped == "":
        return None
    try:
        return float(stripped)
    except ValueError:
        pass

    allowed = "".join(ch for ch in stripped if ch.isdigit() or ch in ".-")
    if allowed in ("", "-", ".", "-."):
        return None
    try:
        return float(allowed)
    except ValueError:
        return None


def pid_format_target_like_current_data(current_data: str, target_value: float) -> str:
    template = current_data.strip()
    if not template:
        raise ValueError("No current PID setpoint template is available.")

    negative = template.startswith("-")
    body = template[1:] if negative else template

    if "." in body:
        left, right = body.split(".", 1)
        decimals = len(right)
        width_left = len(left)
        formatted = f"{round(target_value, decimals):0{width_left}.{decimals}f}"
        if target_value < 0 and not formatted.startswith("-"):
            formatted = "-" + formatted
        return formatted

    width = len(body)
    if not float(target_value).is_integer():
        raise ValueError(
            f"The PID returned S1 without decimals ('{template}'); target must be an integer."
        )
    integer_value = int(round(target_value))
    sign = "-" if integer_value < 0 else ""
    digits = str(abs(integer_value)).zfill(width)
    return sign + digits


class PIDController:
    def __init__(self, port: str, baudrate: int, address: str, state: SharedState) -> None:
        self.state = state
        self.address = address
        self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=1)

    def read_identifier_raw(self, identifier: str, wait_s: float = 0.15) -> bytes:
        with self.state.pid_lock:
            self.ser.reset_input_buffer()
            self.ser.write(PID_EOT)
            time.sleep(0.05)
            self.ser.write(self.address.encode("ascii") + identifier.encode("ascii") + PID_ENQ)
            time.sleep(wait_s)
            return self.ser.read(self.ser.in_waiting or 64)

    def read_value(self, identifier: str) -> tuple[dict, Optional[float]]:
        parsed = pid_parse_frame(self.read_identifier_raw(identifier))
        value = None
        if parsed.get("status") == "DATA":
            value = pid_parse_numeric_ascii(parsed.get("data", ""))
        return parsed, value

    def write_s1(self, data_text: str) -> bytes:
        body = b"S1" + data_text.encode("ascii") + PID_ETX
        frame = PID_EOT + self.address.encode("ascii") + PID_STX + body + pid_xor_bcc(body)
        with self.state.pid_lock:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            time.sleep(0.20)
            return self.ser.read(1)

    @staticmethod
    def ack_name(raw: bytes) -> str:
        if raw == PID_ACK:
            return "ACK"
        if raw == PID_NAK:
            return "NAK"
        if raw == PID_EOT:
            return "EOT"
        if raw == b"":
            return "NO_RESPONSE"
        return repr(raw)

    def set_setpoint_c(self, target_c: float) -> None:
        banner(f"Setting Oven PID target temperature to {target_c:.1f} °C")
        s1_before, sv_before = self.read_value("S1")

        if s1_before.get("status") != "DATA" or sv_before is None:
            raise RuntimeError(
                f"Could not read PID setpoint S1 before writing. status={s1_before.get('status')} raw={s1_before.get('raw')}"
            )

        data_text = pid_format_target_like_current_data(s1_before["data"], target_c)
        write_reply = self.write_s1(data_text)
        time.sleep(0.20)

        _, sv_after = self.read_value("S1")
        _, pv_after = self.read_value("M1")

        if sv_after is None or abs(sv_after - target_c) > 0.51:
            raise RuntimeError(
                f"PID did not confirm the requested setpoint. Reply={self.ack_name(write_reply)}, readback={sv_after}"
            )

        self.state.oven_setpoint_c = sv_after
        info(
            f"Oven PID target confirmed: PV={pv_after if pv_after is not None else '--'} °C | "
            f"SV={sv_after:.1f} °C | reply={self.ack_name(write_reply)}",
            Fore.MAGENTA,
        )

    def set_setpoint_c_best_effort(self, target_c: float) -> None:
        banner(f"Best-effort abort action: setting Oven PID target temperature to {target_c:.1f} °C")
        s1_before, sv_before = self.read_value("S1")

        if s1_before.get("status") != "DATA" or sv_before is None:
            raise RuntimeError(
                f"Could not read PID setpoint S1 before writing during abort. status={s1_before.get('status')} raw={s1_before.get('raw')}"
            )

        data_text = pid_format_target_like_current_data(s1_before["data"], target_c)
        write_reply = self.write_s1(data_text)
        self.state.oven_setpoint_c = target_c

        reply_name = self.ack_name(write_reply)
        if write_reply != PID_ACK:
            raise RuntimeError(f"PID abort write did not return ACK. Reply={reply_name}")

        info(
            f"Abort PID write sent successfully: target={target_c:.1f} °C | reply={reply_name}",
            Fore.MAGENTA,
        )

    def close(self) -> None:
        reset_serial_buffers_and_close(self.ser, "Oven PID port")


# =============================================================================
# KEYSIGHT COMMUNICATION
# =============================================================================
class KeysightController:
    def __init__(self, port: str, baudrate: int, state: SharedState) -> None:
        self.state = state
        self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=1)

    def write(self, command: str, delay: float = 0.10) -> None:
        with self.state.keysight_lock:
            self.ser.write((command + "\n").encode())
            time.sleep(delay)

    def query(self, command: str, delay: float = 0.10) -> str:
        with self.state.keysight_lock:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\n").encode())
            time.sleep(delay)
            return self.ser.readline().decode(errors="ignore").strip()

    def measure_current_a(self) -> Optional[float]:
        try:
            return float(self.query("MEAS:CURR?"))
        except Exception:
            return None

    def measure_voltage_v(self) -> Optional[float]:
        try:
            return float(self.query("MEAS:VOLT?"))
        except Exception:
            return None

    def set_current_a(self, current_a: float) -> None:
        current_a = max(0.0, current_a)
        self.write(f"CURR {current_a:.3f}")
        self.state.last_keysight_set_current_a = current_a

    def configure_for_remote_rampdown(self) -> None:
        self.write("SYST:REM")
        self.write(f"VOLT:RANG {KEYSIGHT_RANGE}")
        self.write("*CLS")
        self.write(f"VOLT:PROT {KEYSIGHT_VOLTAGE_LIMIT_V:.3f}")
        self.write("VOLT:PROT:STAT ON")
        self.write(f"CURR:PROT {KEYSIGHT_OCP_A:.3f}")
        self.write("CURR:PROT:STAT ON")
        self.write(f"VOLT {KEYSIGHT_VOLTAGE_LIMIT_V:.3f}")
        self.write("OUTP ON")

    def shutdown_output(self) -> None:
        self.write("SYST:REM")
        self.write("CURR 0.000")
        self.write("OUTP OFF")
        self.state.last_keysight_set_current_a = 0.0
        self.state.final_keysight_current_a = 0.0

    def close(self) -> None:
        reset_serial_buffers_and_close(self.ser, "Keysight power-supply port")


# =============================================================================
# DATA LOGGER
# =============================================================================
class AnnealLogger:
    def __init__(self, run_name: str, output_dir: str) -> None:
        self.run_name = run_name
        self.output_dir = output_dir
        date_str = datetime.now().strftime("%Y-%m-%d")
        # Raw PID data are stored as data files. The plotted curve is saved separately
        # as a clean PNG at the end of the run.
        self.sqlite_path = os.path.join(output_dir, f"{run_name}___PID_temperature_data___{date_str}.db")
        self.pid_temperature_csv_path = os.path.join(output_dir, "01__PID_temperature_data.csv")
        self.csv_path = os.path.join(output_dir, f"{run_name}_telemetry.csv")

        self.conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS temperatures (
                timestamp TEXT,
                temperature REAL
            )
            """
        )
        self.conn.commit()

        self.pid_temperature_csv_file = open(self.pid_temperature_csv_path, "w", newline="", encoding="utf-8")
        self.pid_temperature_csv_writer = csv.writer(self.pid_temperature_csv_file)
        self.pid_temperature_csv_writer.writerow(["timestamp", "oven_temperature_c"])
        self.pid_temperature_csv_file.flush()

        self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "timestamp",
            "phase",
            "oven_temperature_c",
            "oven_setpoint_c",
            "ck1_temperature_c",
            "raw_pyrometer_c",
            "estimated_sample_c",
            "pyrometer_status",
            "keysight_current_a",
            "keysight_voltage_v",
            "message",
        ])
        self.csv_file.flush()

    def log_temperature(self, timestamp: datetime, temperature_c: float) -> None:
        timestamp_text = timestamp.isoformat()
        self.cursor.execute(
            "INSERT INTO temperatures (timestamp, temperature) VALUES (?, ?)",
            (timestamp_text, temperature_c),
        )
        self.conn.commit()
        self.pid_temperature_csv_writer.writerow([timestamp_text, temperature_c])
        self.pid_temperature_csv_file.flush()

    def log_row(
        self,
        timestamp: datetime,
        phase: str,
        oven_temperature_c: Optional[float],
        oven_setpoint_c: Optional[float],
        keysight_current_a: Optional[float],
        keysight_voltage_v: Optional[float],
        message: str,
        ck1_temperature_c: Optional[float] = None,
        raw_pyrometer_c: Optional[float] = None,
        estimated_sample_c: Optional[float] = None,
        pyrometer_status: str = "",
    ) -> None:
        self.csv_writer.writerow([
            timestamp.isoformat(),
            phase,
            oven_temperature_c,
            oven_setpoint_c,
            ck1_temperature_c,
            raw_pyrometer_c,
            estimated_sample_c,
            pyrometer_status,
            keysight_current_a,
            keysight_voltage_v,
            message,
        ])
        self.csv_file.flush()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.pid_temperature_csv_file.close()
        except Exception:
            pass
        try:
            self.csv_file.close()
        except Exception:
            pass


# =============================================================================
# MONITOR THREADS
# =============================================================================
def monitor_oven(pid: PIDController, logger: AnnealLogger, state: SharedState) -> None:
    while not state.stop_event.is_set():
        try:
            parsed, temperature_value = pid.read_value("M1")
            if temperature_value is None:
                raise ValueError(
                    f"Could not parse Oven PID PV from frame: status={parsed.get('status')} raw={parsed.get('raw')}"
                )
            ts, formatted, dec = log_ts()
            with state.data_lock:
                state.oven_temperature_times.append(ts)
                state.oven_temperature_values.append(temperature_value)
            logger.log_temperature(ts, temperature_value)
            info(f"Oven PID temperature: {temperature_value:.1f} °C", Fore.MAGENTA)
        except serial.SerialException as exc:
            info(f"Serial error in Oven PID monitor: {exc}", Fore.RED)
        except Exception as exc:
            info(f"Error in Oven PID monitor: {exc}", Fore.RED)
        if not sleep_interruptibly(READ_INTERVAL_S, state.stop_event):
            break


def monitor_pyrometer(state: SharedState) -> None:
    """Monitoring-only COM10 reader; it never changes annealing control decisions.

    Setup warnings never disable temperature data.
    """

    if not PYROMETER_PROFILE.enabled:
        state.pyrometer_status = "disabled by launcher profile"
        info("IMPAC pyrometer monitoring is disabled for this launcher run.", Fore.CYAN)
        return

    emissivity_setup_attempted = False
    while not state.stop_event.is_set():
        reader = ImpacIPE140(PYROMETER_SERIAL_CONFIG)
        try:
            reader.open()
            state.pyrometer_status = "connected"
            state.pyrometer_last_error = ""

            try:
                if PYROMETER_PROFILE.write_emissivity_at_start and not emissivity_setup_attempted:
                    emissivity_setup_attempted = True
                    confirmed, changed = reader.ensure_emissivity_percent(PYROMETER_PROFILE.emissivity_percent)
                else:
                    confirmed = reader.read_emissivity_percent()
                    changed = False
                state.pyrometer_confirmed_emissivity_percent = confirmed
                action = "updated and verified" if changed else "verified"
                info(f"IMPAC pyrometer emissivity {action}: {confirmed:.0f}%.", Fore.CYAN)
            except Exception as exc:
                state.pyrometer_confirmed_emissivity_percent = None
                state.pyrometer_last_error = f"Emissivity setup warning: {exc}"
                info(
                    f"Pyrometer emissivity setup warning; temperature monitoring continues: {exc}",
                    Fore.YELLOW,
                )

            while not state.stop_event.is_set():
                try:
                    raw_c = reader.read_temperature_c()
                    sample_c = PYROMETER_PROFILE.estimated_sample_c(raw_c)
                    status = PYROMETER_PROFILE.calibration_status(raw_c)
                    ts = datetime.now()
                    with state.data_lock:
                        state.pyrometer_temperature_times.append(ts)
                        state.pyrometer_temperature_values.append(raw_c)
                        state.sample_temperature_estimates.append(sample_c)
                        state.pyrometer_status_values.append(status)
                    state.pyrometer_status = "connected" if status == "OK" else "connected; extrapolating"
                    warning = "" if status == "OK" else " | WARNING: extrapolated below calibrated range"
                    info(
                        f"Pyrometer: {raw_c:.1f} °C | Estimated sample: {sample_c:.1f} °C{warning}",
                        Fore.CYAN,
                    )
                except Exception as exc:
                    state.pyrometer_status = "temporarily unavailable"
                    state.pyrometer_last_error = str(exc)
                    raise
                if not sleep_interruptibly(1.0, state.stop_event):
                    break
        except Exception as exc:
            state.pyrometer_status = "unavailable; retrying"
            state.pyrometer_last_error = str(exc)
            info(
                f"IMPAC pyrometer unavailable on {PYROMETER_SERIAL_CONFIG.port}; retrying in 5 s: {exc}",
                Fore.YELLOW,
            )
        finally:
            try:
                reader.close()
            except Exception:
                pass

        if not sleep_interruptibly(5.0, state.stop_event):
            break


def monitor_ck1_temperature(state: SharedState) -> None:
    """Read the Arduino CK-1 crucible temperature, as in the heat-up scripts."""
    try:
        ser = serial.Serial(port=CK1_ARDUINO_PORT, baudrate=CK1_ARDUINO_BAUDRATE, timeout=1)
    except serial.SerialException as exc:
        info(f"CK-1 Arduino temperature monitor unavailable on {CK1_ARDUINO_PORT}: {exc}", Fore.RED)
        return

    try:
        while not state.stop_event.is_set():
            try:
                raw = ser.readline().decode(errors="ignore").strip()
                if raw:
                    temperature_value = float(raw)
                    ts, formatted, dec = log_ts()
                    with state.data_lock:
                        state.ck1_temperature_times.append(ts)
                        state.ck1_temperature_values.append(temperature_value)
                    info(f"CK-1 crucible temperature: {temperature_value:.2f} °C", Fore.RED)
                else:
                    time.sleep(0.1)
            except ValueError:
                info(f"CK-1 Arduino temperature parse failed from: {raw!r}", Fore.RED)
            except serial.SerialException as exc:
                info(f"Serial error in CK-1 temperature monitor: {exc}", Fore.RED)
                break
            except Exception as exc:
                info(f"Error in CK-1 temperature monitor: {exc}", Fore.RED)
                time.sleep(0.5)
    finally:
        reset_serial_buffers_and_close(ser, "Arduino CK-1 temperature port")


def monitor_keysight(keysight: KeysightController, state: SharedState) -> None:
    while not state.stop_event.is_set():
        try:
            measured_current = keysight.measure_current_a()
            measured_voltage = keysight.measure_voltage_v()
            ts, formatted, dec = log_ts()

            if measured_current is not None:
                with state.data_lock:
                    state.keysight_current_times.append(ts)
                    state.keysight_current_values.append(measured_current)
                info(f"Keysight current: {measured_current:.4f} A", Fore.RED)

            if measured_voltage is not None:
                with state.data_lock:
                    state.keysight_voltage_times.append(ts)
                    state.keysight_voltage_values.append(measured_voltage)
                info(f"Keysight voltage: {measured_voltage:.4f} V", Fore.YELLOW)

            if (
                measured_current is not None
                and measured_current <= KEYSIGHT_ZERO_THRESHOLD_A
                and not state.current_zero_notice_shown
            ):
                state.final_keysight_current_a = measured_current
                if not state.keysight_output_off_at_zero:
                    try:
                        keysight.shutdown_output()
                        state.keysight_output_off_at_zero = True
                        info("Keysight output automatically switched OFF before the evaporator switch-off prompt.", Fore.YELLOW)
                    except Exception as exc:
                        info(f"Could not automatically switch Keysight output OFF at zero current: {exc}", Fore.RED)
                state.show_switch_off_prompt()
                banner(STATUS_MESSAGE_CURRENT_ZERO)

            if state.current_zero_notice_shown:
                if measured_current is None and measured_voltage is None:
                    state.consecutive_missing_evaporator_reads += 1
                else:
                    state.consecutive_missing_evaporator_reads = 0

                if state.consecutive_missing_evaporator_reads >= 2:
                    state.clear_switch_off_prompt()
                    banner("Evaporator switched off detected. Hiding the switch-off warning.")
        except serial.SerialException as exc:
            info(f"Serial error in Keysight monitor: {exc}", Fore.RED)
        except Exception as exc:
            info(f"Error in Keysight monitor: {exc}", Fore.RED)

        if not sleep_interruptibly(READ_INTERVAL_S, state.stop_event):
            break


def rampdown_keysight(keysight: KeysightController, state: SharedState) -> None:
    try:
        keysight.configure_for_remote_rampdown()
        start_current = state.initial_keysight_current_a
        if start_current is None:
            start_current = keysight.measure_current_a()
        if start_current is None:
            raise RuntimeError("Could not determine the initial Keysight current for the rampdown.")
        start_current = max(0.0, start_current)
        state.initial_keysight_current_a = start_current
        state.last_keysight_set_current_a = start_current

        banner(
            f"Keysight rampdown started in parallel with the annealing sequence from {start_current:.3f} A. "
            f"Target ramp: -{KEYSIGHT_RAMPDOWN_STEP_A:.2f} A every {KEYSIGHT_RAMPDOWN_STEP_S/60:.0f} min."
        )
        state.set_message("Annealing and rampdown are running together.")

        if FIRST_RAMPDOWN_STEP_DELAY_S > 0:
            if not sleep_interruptibly(FIRST_RAMPDOWN_STEP_DELAY_S, state.stop_event):
                return

        current_setpoint = start_current
        while not state.stop_event.is_set() and current_setpoint > 0.0:
            next_current = max(0.0, round(current_setpoint - KEYSIGHT_RAMPDOWN_STEP_A, 3))
            keysight.set_current_a(next_current)
            state.last_keysight_set_current_a = next_current
            info(
                f"Keysight rampdown step: {current_setpoint:.3f} A -> {next_current:.3f} A",
                Fore.YELLOW,
            )
            current_setpoint = next_current

            if current_setpoint <= 0.0:
                state.final_keysight_current_a = 0.0
                if not state.keysight_output_off_at_zero:
                    try:
                        keysight.shutdown_output()
                        state.keysight_output_off_at_zero = True
                        info("Keysight output automatically switched OFF after rampdown reached 0 A.", Fore.YELLOW)
                    except Exception as exc:
                        info(f"Could not switch Keysight output OFF after rampdown reached 0 A: {exc}", Fore.RED)
                state.show_switch_off_prompt()
                banner(STATUS_MESSAGE_CURRENT_ZERO)
                break

            if not sleep_interruptibly(KEYSIGHT_RAMPDOWN_STEP_S, state.stop_event):
                return
    except serial.SerialException as exc:
        info(f"Serial error in Keysight rampdown: {exc}", Fore.RED)
        state.stop_event.set()
    except Exception as exc:
        info(f"Error in Keysight rampdown: {exc}", Fore.RED)
        state.stop_event.set()
    finally:
        state.rampdown_finished_event.set()


# =============================================================================
# ANNEALING SEQUENCE (converted from your notebook, now pure .py)
# =============================================================================
def latest_oven_temperature_c(state: SharedState) -> Optional[float]:
    with state.data_lock:
        return state.oven_temperature_values[-1] if state.oven_temperature_values else None


def latest_ck1_temperature_c(state: SharedState) -> Optional[float]:
    with state.data_lock:
        return state.ck1_temperature_values[-1] if state.ck1_temperature_values else None


def latest_oven_temperature_age_s(state: SharedState) -> Optional[float]:
    with state.data_lock:
        timestamp = state.oven_temperature_times[-1] if state.oven_temperature_times else None
    if timestamp is None:
        return None
    try:
        return max(0.0, time.time() - timestamp.timestamp())
    except Exception:
        return None


def require_fresh_oven_signal(state: SharedState, *, allow_initial_grace_s: float = 0.0, started_mono: Optional[float] = None) -> None:
    age_s = latest_oven_temperature_age_s(state)
    if age_s is None and started_mono is not None and time.monotonic() - started_mono <= allow_initial_grace_s:
        return
    if age_s is None or age_s > OVEN_SIGNAL_STALE_TIMEOUT_S:
        raise RuntimeError(
            'Oven PID process-value signal is unavailable or stale '
            f'(age={age_s if age_s is not None else "unknown"} s; '
            f'limit={OVEN_SIGNAL_STALE_TIMEOUT_S:.1f} s).'
        )


def stage_reached_threshold(target_c: float) -> float:
    """Lower edge retained for display compatibility; readiness is symmetric."""
    return float(target_c) - abs(float(STAGE_REACHED_MARGIN_C))


def wait_until_temperature_reached(
    state: SharedState,
    target_getter,
    label_getter,
) -> bool:
    tolerance = abs(float(STAGE_REACHED_MARGIN_C))
    required_s = max(0.0, float(STAGE_STABLE_DURATION_S))
    banner(
        f"Waiting until the oven is stable inside {label_getter()} "
        f"(±{tolerance:.1f} °C for {required_s:.0f} s)."
    )
    state.set_message(f"Waiting for stable {label_getter()}")
    stable_since = None
    last_target = None

    while not state.stop_event.is_set():
        target_c = float(target_getter())
        if last_target is None or not math.isclose(target_c, last_target, abs_tol=1e-9):
            stable_since = None
            last_target = target_c
        current_temp = latest_oven_temperature_c(state)
        require_fresh_oven_signal(state)
        now_mono = time.monotonic()
        inside = current_temp is not None and abs(float(current_temp) - target_c) <= tolerance
        if inside:
            if stable_since is None:
                stable_since = now_mono
        else:
            stable_since = None
        stable_elapsed = 0.0 if stable_since is None else now_mono - stable_since
        state.set_message(
            f"Waiting for {label_getter()} | band {target_c:.1f} ± {tolerance:.1f} °C | "
            f"oven {current_temp if current_temp is not None else '--'} °C | "
            f"stable {stable_elapsed:.0f}/{required_s:.0f} s"
        )
        if inside and stable_elapsed >= required_s:
            info(
                f"Oven stable at {current_temp:.1f} °C inside {target_c:.1f} ± {tolerance:.1f} °C "
                f"for {required_s:.0f} s.",
                Fore.MAGENTA,
            )
            return True
        time.sleep(2.0)
    return False


def hold_for_seconds(state: SharedState, seconds: float, label: str) -> bool:
    banner(f"{label} for {seconds / 60:.1f} min")
    duration_s = max(0.0, float(seconds))
    start_mono = time.monotonic()
    with state.state_lock:
        state.phase_deadline_at = time.time() + duration_s
        state.last_message = label
    while not state.stop_event.is_set():
        require_fresh_oven_signal(
            state,
            allow_initial_grace_s=OVEN_SIGNAL_INITIAL_GRACE_S,
            started_mono=start_mono,
        )
        elapsed = time.monotonic() - start_mono
        if elapsed >= duration_s:
            return True
        time.sleep(min(0.25, max(0.05, duration_s - elapsed)))
    return False


def hold_with_live_duration(
    state: SharedState,
    duration_getter,
    label_getter,
    target_getter=None,
) -> bool:
    initial_duration_s = max(0.0, float(duration_getter()))
    banner(f"{label_getter()} for {initial_duration_s / 60:.1f} effective min")
    effective_elapsed_s = 0.0
    last_mono = time.monotonic()
    tolerance = abs(float(STAGE_REACHED_MARGIN_C))

    while not state.stop_event.is_set():
        duration_s = max(0.0, float(duration_getter()))
        label = label_getter()
        now_mono = time.monotonic()
        dt = max(0.0, now_mono - last_mono)
        last_mono = now_mono
        current_temp = latest_oven_temperature_c(state)
        require_fresh_oven_signal(state)
        target_c = float(target_getter()) if target_getter is not None else None
        inside = (
            target_c is None
            or (current_temp is not None and abs(float(current_temp) - target_c) <= tolerance)
        )
        if inside or not PAUSE_HOLD_OUTSIDE_TEMPERATURE_BAND:
            effective_elapsed_s += dt
        remaining_s = max(0.0, duration_s - effective_elapsed_s)

        with state.state_lock:
            state.phase_deadline_at = time.time() + remaining_s
            state.last_message = (
                f"{label} | effective hold {duration_s / 60:.1f} min | "
                f"remaining {remaining_s / 60:.1f} min | in band: {inside}"
            )

        if effective_elapsed_s >= duration_s:
            return True
        time.sleep(min(0.25, max(0.05, remaining_s)))

    return False


def run_annealing_sequence(pid: PIDController, state: SharedState) -> None:
    try:
        # Initial wait is now an active 200 °C oven setpoint, not only a passive wait.
        state.set_phase(
            "INITIAL_WAIT",
            f"Initial wait at {INITIAL_WAIT_TARGET_C:.0f} °C before the first oven ramp",
            duration_s=INITIAL_WAIT_S,
        )
        pid.set_setpoint_c(INITIAL_WAIT_TARGET_C)
        if not hold_for_seconds(state, INITIAL_WAIT_S, f"Initial wait at {INITIAL_WAIT_TARGET_C:.0f} °C"):
            return

        first_target = state.get_first_stage_target_c()
        state.set_phase("RAMP_TO_FIRST", f"Setting PID target to first-stage value: {first_target:.1f} °C")
        pid.set_setpoint_c(first_target)
        if not wait_until_temperature_reached(
            state,
            state.get_first_stage_target_c,
            lambda: f"the first stage ({state.get_first_stage_target_c():.1f} °C)",
        ):
            return

        first_hold_s = state.get_first_stage_hold_s()
        state.set_phase(
            "HOLD_FIRST",
            f"Holding first-stage value: {state.get_first_stage_target_c():.1f} °C",
            duration_s=first_hold_s,
        )
        if not hold_with_live_duration(
            state,
            state.get_first_stage_hold_s,
            lambda: f"Holding first stage at {state.get_first_stage_target_c():.1f} °C",
            state.get_first_stage_target_c,
        ):
            return

        second_target = state.get_second_stage_target_c()
        state.set_phase("RAMP_TO_SECOND", f"Setting PID target to second-stage value: {second_target:.1f} °C")
        pid.set_setpoint_c(second_target)
        if not wait_until_temperature_reached(
            state,
            state.get_second_stage_target_c,
            lambda: f"the second stage ({state.get_second_stage_target_c():.1f} °C)",
        ):
            return

        second_hold_s = state.get_second_stage_hold_s()
        state.set_phase(
            "HOLD_SECOND",
            f"Holding second-stage value: {state.get_second_stage_target_c():.1f} °C",
            duration_s=second_hold_s,
        )
        if not hold_with_live_duration(
            state,
            state.get_second_stage_hold_s,
            lambda: f"Holding second stage at {state.get_second_stage_target_c():.1f} °C",
            state.get_second_stage_target_c,
        ):
            return

        state.set_phase("RAMP_TO_0", "Setting oven PID setpoint to 0 °C")
        pid.set_setpoint_c(COOLDOWN_TARGET_C)

        final_hold_message = (
            f"Holding oven PID setpoint at {COOLDOWN_TARGET_C:.0f} °C "
            f"for {POST_COOLDOWN_WAIT_S / 60.0:.1f} min before finishing"
        )
        state.set_phase(
            "HOLD_0_AND_FINALIZE",
            final_hold_message,
            duration_s=POST_COOLDOWN_WAIT_S,
        )
        if not hold_for_seconds(state, POST_COOLDOWN_WAIT_S, final_hold_message):
            return

        state.anneal_finished_normally = True
        state.finished_event.set()
        state.set_phase(
            "FINISHED",
            f"Annealing finished normally. Oven PID setpoint remains at {COOLDOWN_TARGET_C:.0f} °C.",
        )
        state.set_message(
            f"Annealing finished. Oven PID setpoint remains at {COOLDOWN_TARGET_C:.0f} °C."
        )
        banner(
            f"NPG annealing sequence finished. Oven PID setpoint remains at "
            f"{COOLDOWN_TARGET_C:.0f} °C."
        )
    except KeyboardInterrupt:
        state.stop_event.set()
        raise
    except Exception as exc:
        state.set_message(f"Annealing sequence failed: {exc}")
        info(f"Error in annealing sequence: {exc}", Fore.RED)
        state.stop_event.set()


# =============================================================================
# CONSOLE COMMANDS
# =============================================================================
def user_command_listener(state: SharedState) -> None:
    while not state.stop_event.is_set():
        try:
            command = input().strip().lower()
        except EOFError:
            break
        except Exception:
            time.sleep(0.2)
            continue

        if command == "q":
            banner("Manual stop requested by user.")
            state.mark_abort("Manual stop requested by user.")
            state.stop_event.set()
            break
        elif command == "i":
            current_temp = latest_oven_temperature_c(state)
            with state.data_lock:
                current_current = state.keysight_current_values[-1] if state.keysight_current_values else None
                current_voltage = state.keysight_voltage_values[-1] if state.keysight_voltage_values else None
            print("\nCurrent status:")
            print(f"  Phase: {state.phase}")
            print(f"  Oven temperature: {current_temp}")
            print(f"  Oven setpoint: {state.oven_setpoint_c}")
            print(f"  Keysight current: {current_current}")
            print(f"  Keysight voltage: {current_voltage}")
            print(f"  Message: {state.last_message}\n")
        elif command == "h":
            print("Commands: 'i' = show status, 'q' = stop")
        elif command:
            print("Commands: 'i' = show status, 'q' = stop")


# =============================================================================
# PLOTTING / INTERFACE
# =============================================================================
def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"


def phase_display_info(state: SharedState) -> tuple[str, str]:
    first = state.get_first_stage_target_c()
    second = state.get_second_stage_target_c()
    mapping = {
        "STARTING": ("STARTING", "#475569"),
        "INITIAL_WAIT": (f"INITIAL WAIT · {INITIAL_WAIT_TARGET_C:.0f} °C", "#2563eb"),
        "RAMP_TO_FIRST": (f"REACHING FIRST STAGE · {first:.0f} °C", "#7c3aed"),
        "HOLD_FIRST": (f"HOLDING FIRST STAGE · {first:.0f} °C", "#0f766e"),
        "RAMP_TO_SECOND": (f"REACHING SECOND STAGE · {second:.0f} °C", "#db2777"),
        "HOLD_SECOND": (f"HOLDING SECOND STAGE · {second:.0f} °C", "#b45309"),
        "RAMP_TO_0": ("REACHING 0 °C", "#0284c7"),
        "HOLD_0_AND_FINALIZE": ("HOLDING 0 °C · FINALIZING", "#0369a1"),
        "FINISHED": (f"FINISHED · PID SV {COOLDOWN_TARGET_C:.0f} °C", "#15803d"),
        "ABORTING": ("ABORTING SAFELY", "#b91c1c"),
    }
    return mapping.get(state.phase, (str(state.phase).replace("_", " "), "#334155"))


def phase_timeline_text(state: SharedState) -> str:
    first = state.get_first_stage_target_c()
    second = state.get_second_stage_target_c()
    first_hold_min = state.get_first_stage_hold_s() / 60.0
    second_hold_min = state.get_second_stage_hold_s() / 60.0
    return (
        f"1. Initial wait at {INITIAL_WAIT_TARGET_C:.0f} °C\n"
        f"2. Reaching first stage: {first:.1f} °C\n"
        f"3. Holding first stage: {first_hold_min:.1f} min\n"
        f"4. Reaching second stage: {second:.1f} °C\n"
        f"5. Holding second stage: {second_hold_min:.1f} min\n"
        f"6. PID SV {COOLDOWN_TARGET_C:.0f} °C: hold "
        f"{POST_COOLDOWN_WAIT_S / 60.0:.1f} min, then finish"
    )


def build_figure() -> tuple[plt.Figure, dict]:
    # Match the Phase 01/02/03 typography on the Windows operator station.
    plt.rcParams["font.family"] = "Segoe UI"
    fig, axes = plt.subplots(2, 2, figsize=(17.2, 9.4))
    fig.patch.set_facecolor("#eef2f7")
    plt.subplots_adjust(left=0.06, right=0.735, top=0.88, bottom=0.085, hspace=0.34, wspace=0.27)

    ax_oven = axes[0, 0]
    ax_ck1 = axes[0, 1]
    ax_current = axes[1, 0]
    ax_voltage = axes[1, 1]

    line_oven, = ax_oven.plot([], [], linewidth=2.2, color="#c62828")
    line_ck1, = ax_ck1.plot([], [], linewidth=2.2, color="#ef4444")
    line_current, = ax_current.plot([], [], linewidth=2.2, color="#d97706")
    line_voltage, = ax_voltage.plot([], [], linewidth=2.2, color="#ca8a04")

    axis_styles = [
        (ax_oven, "Oven PID temperature", "Temperature (°C)", "#c62828"),
        (ax_ck1, "CK-1 temp", "Temperature (°C)", "#dc2626"),
        (ax_current, "Evaporator current", "Current (A)", "#b45309"),
        (ax_voltage, "Evaporator voltage", "Voltage (V)", "#854d0e"),
    ]
    for ax, title, ylabel, title_color in axis_styles:
        ax.set_title(title, fontsize=12, fontweight="bold", color=title_color, pad=10)
        ax.set_xlabel("Time", fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        ax.tick_params(axis="x", rotation=25, labelsize=9)
        ax.tick_params(axis="y", labelsize=9)
        ax.grid(True, alpha=0.22, color="#9fb3c8")
        ax.set_facecolor("white")
        for spine in ax.spines.values():
            spine.set_color("#d6dee8")
            spine.set_linewidth(1.15)

    temperature_view_buttons = {}
    bbox = ax_oven.get_position()
    gap = 0.004
    button_width = (bbox.width - 2 * gap) / 3.0
    selector_y = min(0.925, bbox.y1 + 0.006)
    selector_height = 0.027
    for index, (mode, label) in enumerate((("oven", "OVEN PID"), ("pyrometer", "PYROMETER"), ("sample", "SAMPLE EST."))):
        selector_ax = fig.add_axes([
            bbox.x0 + index * (button_width + gap),
            selector_y,
            button_width,
            selector_height,
        ])
        button = Button(selector_ax, label, color="#edf1f6", hovercolor="#ffffff")
        button.label.set_fontsize(8.0)
        button.label.set_fontweight("bold")
        temperature_view_buttons[mode] = button

    phase_title_text = fig.text(
        0.50,
        0.965,
        "STARTING",
        ha="center",
        va="center",
        fontsize=20,
        fontweight="bold",
        color="white",
        bbox=dict(boxstyle="round,pad=0.45", facecolor="#475569", edgecolor="#475569", alpha=0.98),
    )

    panel_left = 0.765
    panel_bottom = 0.055
    panel_width = 0.215
    panel_height = 0.84
    panel_ax = fig.add_axes([panel_left, panel_bottom, panel_width, panel_height])
    panel_ax.set_facecolor("#ffffff")
    for spine in panel_ax.spines.values():
        spine.set_color("#cbd5e1")
        spine.set_linewidth(1.1)
    panel_ax.set_xticks([])
    panel_ax.set_yticks([])

    def panel_text(x, y, text, fontsize=8.6, color="#334155", weight="normal", ha="left"):
        return panel_ax.text(
            x, y, text, transform=panel_ax.transAxes,
            fontsize=fontsize, color=color, fontweight=weight,
            va="top", ha=ha,
        )

    def add_button(x_rel, y_rel, w_rel, h_rel, label, color, hovercolor, text_color, fontsize=8.0):
        ax_button = fig.add_axes([
            panel_left + panel_width * x_rel,
            panel_bottom + panel_height * y_rel,
            panel_width * w_rel,
            panel_height * h_rel,
        ])
        button = Button(ax_button, label, color=color, hovercolor=hovercolor)
        button.label.set_fontsize(fontsize)
        button.label.set_color(text_color)
        return button

    def add_textbox(label, initial, y_rel):
        panel_text(0.05, y_rel + 0.037, label, fontsize=8.2, color="#334155")
        ax_box = fig.add_axes([
            panel_left + panel_width * 0.55,
            panel_bottom + panel_height * y_rel,
            panel_width * 0.36,
            panel_height * 0.030,
        ])
        ax_box.set_facecolor("white")
        textbox = TextBox(ax_box, "", initial=initial)
        textbox.label.set_visible(False)
        textbox.text_disp.set_fontsize(8.8)
        return textbox

    panel_text(0.05, 0.985, "NPG Annealings", fontsize=12.4, color="#0f172a", weight="bold")
    panel_text(0.05, 0.957, "Live chamber control and monitoring", fontsize=8.2, color="#64748b")

    panel_text(0.05, 0.915, "Editable oven PID targets", fontsize=9.4, color="#334155", weight="bold")
    textbox_first = add_textbox("First stage target (°C)", f"{FIRST_STAGE_TARGET_C:.1f}", 0.858)
    button_first = add_button(0.08, 0.810, 0.84, 0.040, "Apply first-stage target", "#dbeafe", "#bfdbfe", "#1e3a8a", fontsize=7.4)

    textbox_second = add_textbox("Second stage target (°C)", f"{SECOND_STAGE_TARGET_C:.1f}", 0.748)
    button_second = add_button(0.08, 0.700, 0.84, 0.040, "Apply second-stage target", "#fce7f3", "#fbcfe8", "#9d174d", fontsize=7.2)

    panel_text(0.05, 0.655, "Editable hold times", fontsize=9.4, color="#334155", weight="bold")
    textbox_first_hold = add_textbox("First hold (min)", f"{FIRST_STAGE_HOLD_S / 60:.1f}", 0.606)
    button_first_hold = add_button(0.08, 0.562, 0.84, 0.036, "Apply first hold time", "#dcfce7", "#bbf7d0", "#166534", fontsize=7.2)

    textbox_second_hold = add_textbox("Second hold (min)", f"{SECOND_STAGE_HOLD_S / 60:.1f}", 0.506)
    button_second_hold = add_button(0.08, 0.462, 0.84, 0.036, "Apply second hold time", "#fef3c7", "#fde68a", "#92400e", fontsize=7.2)

    button_abort = add_button(0.08, 0.400, 0.84, 0.046, "ABORT / SAFE STOP", "#fee2e2", "#fecaca", "#991b1b", fontsize=8.4)

    panel_text(0.05, 0.376, "Current status", fontsize=9.8, color="#334155", weight="bold")
    status_text = panel_ax.text(
        0.05, 0.352, "Initializing...", transform=panel_ax.transAxes,
        fontsize=6.75, color="#0f172a", va="top", ha="left", linespacing=1.02,
        bbox=dict(boxstyle="round,pad=0.36", facecolor="white", edgecolor="#d0d7de", linewidth=1.0),
    )

    panel_text(0.05, 0.145, "Phase sequence", fontsize=8.7, color="#334155", weight="bold")
    timeline_text = panel_ax.text(
        0.05, 0.125, "", transform=panel_ax.transAxes,
        fontsize=5.95, color="#475569", va="top", ha="left", linespacing=1.00,
    )

    panel_text(0.05, 0.035, "Last action", fontsize=8.7, color="#334155", weight="bold")
    action_text = panel_ax.text(
        0.05, 0.017, "Ready.", transform=panel_ax.transAxes,
        fontsize=5.95, color="#334155", va="top", ha="left", linespacing=1.00,
    )

    artists = {
        "ax_oven": ax_oven,
        "ax_ck1": ax_ck1,
        "ax_current": ax_current,
        "ax_voltage": ax_voltage,
        "line_oven": line_oven,
        "temperature_view_buttons": temperature_view_buttons,
        "line_ck1": line_ck1,
        "line_current": line_current,
        "line_voltage": line_voltage,
        "phase_title_text": phase_title_text,
        "status_text": status_text,
        "timeline_text": timeline_text,
        "action_text": action_text,
        "textbox_first": textbox_first,
        "textbox_second": textbox_second,
        "textbox_first_hold": textbox_first_hold,
        "textbox_second_hold": textbox_second_hold,
        "button_first": button_first,
        "button_second": button_second,
        "button_first_hold": button_first_hold,
        "button_second_hold": button_second_hold,
        "button_abort": button_abort,
    }
    return fig, artists


def _finite_series(values):
    finite = []
    for value in values:
        try:
            finite.append(float(value))
        except Exception:
            pass
    return finite


def _set_series(ax, line, times, values):
    n = min(len(times), len(values))
    if n <= 0:
        return
    times = times[:n]
    values = values[:n]
    line.set_data(times, values)
    if n == 1:
        ax.set_xlim(times[0] - timedelta(seconds=30), times[0] + timedelta(seconds=30))
    else:
        ax.set_xlim(min(times), max(times))
    finite = _finite_series(values)
    if finite:
        ymin, ymax = min(finite), max(finite)
        span = ymax - ymin
        if span <= 0:
            span = max(abs(ymax) * 0.12, 1.0)
            ymin -= span / 2
            ymax += span / 2
        margin = max(span * 0.12, 0.1)
        if ax.get_ylabel() in ("Current (A)", "Voltage (V)"):
            ax.set_ylim(max(0.0, ymin - margin), ymax + margin)
        else:
            ax.set_ylim(ymin - margin, ymax + margin)


def update_figure(fig: plt.Figure, artists: dict, state: SharedState) -> None:
    with state.data_lock:
        oven_times = list(state.oven_temperature_times)
        oven_values = list(state.oven_temperature_values)
        pyro_times = list(state.pyrometer_temperature_times)
        pyro_values = list(state.pyrometer_temperature_values)
        sample_values = list(state.sample_temperature_estimates)
        ck1_times = list(state.ck1_temperature_times)
        ck1_values = list(state.ck1_temperature_values)
        current_times = list(state.keysight_current_times)
        current_values = list(state.keysight_current_values)
        voltage_times = list(state.keysight_voltage_times)
        voltage_values = list(state.keysight_voltage_values)

    mode = state.temperature_view_mode
    if mode == "pyrometer":
        selected_times, selected_values = pyro_times, pyro_values
        selected_title = f"Raw pyrometer temperature · ε {PYROMETER_PROFILE.emissivity_percent:.0f}%"
        selected_color = "#1565c0"
    elif mode == "sample":
        selected_times, selected_values = pyro_times, sample_values
        selected_title = f"Estimated sample temperature · {PYROMETER_PROFILE.profile_name}"
        selected_color = "#d4a000"
    else:
        selected_times, selected_values = oven_times, oven_values
        selected_title = "Oven PID temperature"
        selected_color = "#c62828"
    artists["line_oven"].set_data([], [])
    artists["line_oven"].set_color(selected_color)
    artists["ax_oven"].set_title(selected_title, fontsize=11, fontweight="bold", color=selected_color, pad=28)
    _set_series(artists["ax_oven"], artists["line_oven"], selected_times, selected_values)
    _set_series(artists["ax_ck1"], artists["line_ck1"], ck1_times, ck1_values)
    _set_series(artists["ax_current"], artists["line_current"], current_times, current_values)
    _set_series(artists["ax_voltage"], artists["line_voltage"], voltage_times, voltage_values)

    latest_oven = oven_values[-1] if oven_values else None
    latest_ck1 = ck1_values[-1] if ck1_values else None
    latest_pyro = pyro_values[-1] if pyro_values else None
    latest_sample = sample_values[-1] if sample_values else None
    latest_current = current_values[-1] if current_values else None
    latest_voltage = voltage_values[-1] if voltage_values else None

    title, color = phase_display_info(state)
    remaining_s = state.phase_remaining_s()
    elapsed_s = state.phase_elapsed_s()
    title_text = title
    if remaining_s is not None:
        title_text = f"{title} · remaining {format_duration(remaining_s)}"
    artists["phase_title_text"].set_text(title_text)
    artists["phase_title_text"].set_bbox(dict(boxstyle="round,pad=0.45", facecolor=color, edgecolor=color, alpha=0.98))

    current_status = state.last_message
    first = state.get_first_stage_target_c()
    second = state.get_second_stage_target_c()
    first_hold_min = state.get_first_stage_hold_s() / 60.0
    second_hold_min = state.get_second_stage_hold_s() / 60.0

    status_lines = [
        f"Phase: {state.phase}",
        f"Elapsed: {format_duration(elapsed_s)}",
        f"Remaining: {format_duration(remaining_s)}" if remaining_s is not None else "Remaining: until threshold / --",
        f"Oven SV: {state.oven_setpoint_c if state.oven_setpoint_c is not None else '--'} °C",
        f"Oven PV: {latest_oven:.1f} °C" if latest_oven is not None else "Oven PV: --",
        f"CK-1 T: {latest_ck1:.1f} °C" if latest_ck1 is not None else "CK-1 T: --",
        f"Pyrometer: {latest_pyro:.1f} °C" if latest_pyro is not None else f"Pyrometer: {state.pyrometer_status}",
        (f"Sample est.: {latest_sample:.1f} °C" + (" (WARNING: extrapolated)" if latest_pyro is not None and latest_pyro < PYROMETER_PROFILE.minimum_valid_pyrometer_c else "")) if latest_sample is not None else "Sample est.: --",
        f"Current: {latest_current:.4f} A" if latest_current is not None else "Current: --",
        f"Voltage: {latest_voltage:.4f} V" if latest_voltage is not None else "Voltage: --",
        f"First target: {first:.1f} °C",
        f"First hold: {first_hold_min:.1f} min",
        f"Second target: {second:.1f} °C",
        f"Second hold: {second_hold_min:.1f} min",
        f"Output OFF at zero: {'yes' if state.keysight_output_off_at_zero else 'pending'}",
        "",
        f"Status: {current_status}",
    ]
    if state.current_zero_notice_shown:
        status_lines += ["", f"⚠ {STATUS_MESSAGE_CURRENT_ZERO}"]
        artists["status_text"].set_color("#b42318")
    elif state.abort_requested:
        artists["status_text"].set_color("#b91c1c")
    else:
        artists["status_text"].set_color("#0f172a")

    artists["status_text"].set_text("\n".join(status_lines))
    artists["timeline_text"].set_text(phase_timeline_text(state))
    artists["action_text"].set_text(state.gui_action_message)

    try:
        fig.canvas.draw_idle()
    except Exception:
        pass


def show_live_interface(fig: plt.Figure) -> None:
    """Show the Matplotlib interface once, without repeatedly raising it above other windows."""
    try:
        plt.show(block=False)
        manager = getattr(fig.canvas, "manager", None)
        window = getattr(manager, "window", None) if manager is not None else None
        if window is not None:
            # Tk backend: explicitly disable always-on-top if available.
            for attr_name in ("wm_attributes", "attributes"):
                attr = getattr(window, attr_name, None)
                if callable(attr):
                    try:
                        attr("-topmost", False)
                    except Exception:
                        pass
            # Qt backend: raise_ is deliberately not called here.
        fig.canvas.draw_idle()
        if hasattr(fig.canvas, "flush_events"):
            fig.canvas.flush_events()
    except Exception as exc:
        info(f"Could not show live interface cleanly: {exc}", Fore.RED)


def safe_gui_refresh(fig: plt.Figure, delay_s: float = 0.02) -> None:
    try:
        if not plt.fignum_exists(fig.number):
            time.sleep(delay_s)
            return
        canvas = getattr(fig, "canvas", None)
        if canvas is not None and hasattr(canvas, "flush_events"):
            canvas.flush_events()
        elif canvas is not None and hasattr(canvas, "start_event_loop"):
            canvas.start_event_loop(min(delay_s, 0.02))
        time.sleep(delay_s)
    except Exception:
        time.sleep(delay_s)


# =============================================================================
# CLEAN STATIC OUTPUT PLOTS
# =============================================================================
def save_pid_temperature_curve(path: str, state: SharedState, run_name: str) -> str:
    """Save a clean standalone PNG for the PID temperature curve.

    This intentionally does not reuse the live GUI figure. The live figure contains
    status text, warnings and multiple panels, which can make a saved single-curve
    output look cramped or messy. This report-style figure only uses the stored PID
    temperature arrays.
    """
    with state.data_lock:
        times = list(state.oven_temperature_times)
        temperatures = list(state.oven_temperature_values)
        pyro_times = list(state.pyrometer_temperature_times)
        pyro_temperatures = list(state.pyrometer_temperature_values)
        sample_temperatures = list(state.sample_temperature_estimates)

    fig_curve, ax = plt.subplots(figsize=(11.5, 6.2), constrained_layout=True)
    fig_curve.patch.set_facecolor("#eef3f8")
    ax.set_facecolor("white")
    ax.set_title(
        f"NPG Annealings · temperature comparison\nRun: {run_name}",
        fontsize=14,
        fontweight="bold",
        color="#0f172a",
        pad=14,
    )
    ax.set_xlabel("Time", fontsize=10.5)
    ax.set_ylabel("Temperature (°C)", fontsize=10.5)
    ax.grid(True, alpha=0.25, color="#9fb3c8")
    for spine in ax.spines.values():
        spine.set_color("#d6dee8")
        spine.set_linewidth(1.1)

    if times and temperatures:
        n = min(len(times), len(temperatures))
        times = times[:n]
        temperatures = temperatures[:n]
        marker = "o" if n <= 80 else None
        markersize = 3.0 if n <= 80 else 0
        ax.plot(times, temperatures, linewidth=1.9, marker=marker, markersize=markersize, label="Oven PID")
        if pyro_times and pyro_temperatures:
            pn = min(len(pyro_times), len(pyro_temperatures))
            ax.plot(pyro_times[:pn], pyro_temperatures[:pn], linewidth=1.7, label="Pyrometer raw")
        if pyro_times and sample_temperatures:
            sn = min(len(pyro_times), len(sample_temperatures))
            ax.plot(pyro_times[:sn], sample_temperatures[:sn], linewidth=1.7, label="Sample estimate")
        ax.legend(loc="best")
        locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.margins(x=0.03, y=0.12)

        start_time = times[0]
        end_time = times[-1]
        duration_min = max(0.0, (end_time - start_time).total_seconds() / 60.0)
        info_text = (
            f"Points: {n}\n"
            f"Start: {start_time:%Y-%m-%d %H:%M:%S}\n"
            f"End: {end_time:%Y-%m-%d %H:%M:%S}\n"
            f"Duration: {duration_min:.1f} min\n"
            f"Final phase: {state.phase}"
        )
        ax.text(
            0.012,
            0.985,
            info_text,
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=9.0,
            color="#334155",
            bbox=dict(boxstyle="round,pad=0.45", facecolor="white", edgecolor="#cbd5e1", alpha=0.96),
        )
    else:
        ax.text(
            0.5,
            0.5,
            "No PID temperature data recorded yet",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=12,
            color="#64748b",
            bbox=dict(boxstyle="round,pad=0.45", facecolor="white", edgecolor="#cbd5e1", alpha=0.96),
        )
        ax.set_xticks([])

    fig_curve.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig_curve)
    return path


# =============================================================================
# OUTPUT SUMMARY
# =============================================================================
def save_run_summary(path: str, state: SharedState) -> None:
    latest_oven = latest_oven_temperature_c(state)
    latest_ck1 = latest_ck1_temperature_c(state)
    with state.data_lock:
        latest_pyro = state.pyrometer_temperature_values[-1] if state.pyrometer_temperature_values else None
        latest_sample = state.sample_temperature_estimates[-1] if state.sample_temperature_estimates else None
    with state.data_lock:
        latest_current = state.keysight_current_values[-1] if state.keysight_current_values else None
        latest_voltage = state.keysight_voltage_values[-1] if state.keysight_voltage_values else None

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"saved_at: {datetime.now().isoformat()}\n")
        fh.write(f"phase: {state.phase}\n")
        fh.write(f"anneal_finished_normally: {state.anneal_finished_normally}\n")
        fh.write(f"oven_setpoint_c: {state.oven_setpoint_c}\n")
        fh.write(f"first_stage_target_c: {state.get_first_stage_target_c()}\n")
        fh.write(f"first_stage_hold_min: {state.get_first_stage_hold_s() / 60.0}\n")
        fh.write(f"second_stage_target_c: {state.get_second_stage_target_c()}\n")
        fh.write(f"second_stage_hold_min: {state.get_second_stage_hold_s() / 60.0}\n")
        fh.write(f"latest_oven_temperature_c: {latest_oven}\n")
        fh.write(f"latest_ck1_temperature_c: {latest_ck1}\n")
        fh.write(f"latest_pyrometer_temperature_c: {latest_pyro}\n")
        fh.write(f"latest_estimated_sample_temperature_c: {latest_sample}\n")
        fh.write(f"pyrometer_status: {state.pyrometer_status}\n")
        fh.write(f"pyrometer_confirmed_emissivity_percent: {state.pyrometer_confirmed_emissivity_percent}\n")
        fh.write(f"initial_keysight_current_a: {state.initial_keysight_current_a}\n")
        fh.write(f"last_keysight_set_current_a: {state.last_keysight_set_current_a}\n")
        fh.write(f"final_keysight_current_a: {state.final_keysight_current_a}\n")
        fh.write(f"latest_measured_current_a: {latest_current}\n")
        fh.write(f"latest_measured_voltage_v: {latest_voltage}\n")
        fh.write(f"message: {state.last_message}\n")


# =============================================================================
# MAIN
# =============================================================================
class App:
    def __init__(self, run_name: str) -> None:
        self.run_name = run_name
        self.output_dir = os.path.join(OUTPUT_BASE_FOLDER, f"NPG Annealings {run_name}")
        os.makedirs(self.output_dir, exist_ok=True)
        try:
            parameter_record_path = write_effective_parameters(
                os.path.join(self.output_dir, "automation_parameters.json"),
                "anneal",
                RUN_AUTOMATION_OVERRIDES,
            )
        except Exception as exc:
            info(f"Could not save effective automation parameters: {exc}", Fore.YELLOW)

        self.state = SharedState()
        try:
            pyrometer_profile_path = os.path.join(self.output_dir, "pyrometer_profile.json")
            with open(pyrometer_profile_path, "w", encoding="utf-8") as fh:
                json.dump(PYROMETER_SETTINGS, fh, indent=2, sort_keys=True)
                fh.write("\n")
        except Exception as exc:
            info(f"Could not save pyrometer profile: {exc}", Fore.YELLOW)
        self.logger = AnnealLogger(run_name, self.output_dir)
        self.pid = PIDController(PID_PORT, PID_BAUDRATE, PID_ADDRESS, self.state)
        self.keysight = KeysightController(KEYSIGHT_PORT, KEYSIGHT_BAUDRATE, self.state)
        self.fig, self.artists = build_figure()
        self._connect_gui_callbacks()
        self.threads: list[threading.Thread] = []

        self.summary_path = os.path.join(self.output_dir, f"{run_name}_summary.txt")
        self.pid_temperature_curve_path = os.path.join(self.output_dir, "01__temperature_comparison.png")
        self.final_plot_path = os.path.join(self.output_dir, f"{run_name}_final_plot.png")
        self.preflight_initial_current_a: Optional[float] = None

    def _connect_gui_callbacks(self) -> None:
        self.artists["button_first"].on_clicked(self.apply_first_stage_target_from_gui)
        self.artists["button_second"].on_clicked(self.apply_second_stage_target_from_gui)
        self.artists["button_first_hold"].on_clicked(self.apply_first_stage_hold_from_gui)
        self.artists["button_second_hold"].on_clicked(self.apply_second_stage_hold_from_gui)
        self.artists["button_abort"].on_clicked(self.request_abort_from_gui)
        for mode, button in self.artists["temperature_view_buttons"].items():
            button.on_clicked(lambda _event, selected=mode: self.set_temperature_view(selected))
        self.set_temperature_view(self.state.temperature_view_mode)
        self.artists["textbox_first"].on_submit(lambda _text: self.apply_first_stage_target_from_gui(None))
        self.artists["textbox_second"].on_submit(lambda _text: self.apply_second_stage_target_from_gui(None))
        self.artists["textbox_first_hold"].on_submit(lambda _text: self.apply_first_stage_hold_from_gui(None))
        self.artists["textbox_second_hold"].on_submit(lambda _text: self.apply_second_stage_hold_from_gui(None))

    def set_temperature_view(self, mode: str) -> None:
        if mode not in {"oven", "pyrometer", "sample"}:
            return
        self.state.temperature_view_mode = mode
        colors = {
            "oven": ("#fde8e8", "#c62828"),
            "pyrometer": ("#e3f0ff", "#1565c0"),
            "sample": ("#fff8cf", "#d4a000"),
        }
        for key, button in self.artists["temperature_view_buttons"].items():
            inactive, active = colors[key]
            selected = key == mode
            button.color = active if selected else inactive
            button.hovercolor = active if selected else "#ffffff"
            button.label.set_color("#ffffff" if selected else "#26384d")
            button.ax.set_facecolor(button.color)
        update_figure(self.fig, self.artists, self.state)
        try:
            self.fig.canvas.draw_idle()
        except Exception:
            pass

    def _parse_target_textbox(self, key: str) -> float:
        raw = self.artists[key].text.strip().replace(",", ".")
        value = float(raw)
        if value < 0:
            raise ValueError("temperature target must be >= 0 °C")
        return value

    def _parse_hold_minutes_textbox(self, key: str) -> float:
        raw = self.artists[key].text.strip().replace(",", ".")
        value = float(raw)
        if value < 0:
            raise ValueError("hold time must be >= 0 min")
        return value

    def _apply_pid_setpoint_if_relevant(self, target_c: float, relevant_phases: tuple[str, ...], label: str) -> None:
        if self.state.phase not in relevant_phases:
            return
        try:
            self.pid.set_setpoint_c(target_c)
            self.state.set_gui_action_message(f"{label} target applied immediately: {target_c:.1f} °C")
        except Exception as exc:
            self.state.set_gui_action_message(f"Could not send {label} target to PID: {exc}")
            info(f"Could not send {label} target to PID: {exc}", Fore.RED)

    def apply_first_stage_target_from_gui(self, event=None) -> None:
        try:
            target_c = self._parse_target_textbox("textbox_first")
            self.state.set_first_stage_target_c(target_c)
            self.state.set_gui_action_message(f"First-stage oven target updated to {target_c:.1f} °C")
            self._apply_pid_setpoint_if_relevant(target_c, ("RAMP_TO_FIRST", "HOLD_FIRST"), "First-stage")
        except Exception as exc:
            self.state.set_gui_action_message(f"Invalid first-stage target: {exc}")
            info(f"Invalid first-stage target from GUI: {exc}", Fore.RED)

    def apply_second_stage_target_from_gui(self, event=None) -> None:
        try:
            target_c = self._parse_target_textbox("textbox_second")
            self.state.set_second_stage_target_c(target_c)
            self.state.set_gui_action_message(f"Second-stage oven target updated to {target_c:.1f} °C")
            self._apply_pid_setpoint_if_relevant(target_c, ("RAMP_TO_SECOND", "HOLD_SECOND"), "Second-stage")
        except Exception as exc:
            self.state.set_gui_action_message(f"Invalid second-stage target: {exc}")
            info(f"Invalid second-stage target from GUI: {exc}", Fore.RED)

    def apply_first_stage_hold_from_gui(self, event=None) -> None:
        try:
            hold_min = self._parse_hold_minutes_textbox("textbox_first_hold")
            self.state.set_first_stage_hold_s(hold_min * 60.0)
            self.state.set_gui_action_message(f"First-stage hold time updated to {hold_min:.1f} min")
        except Exception as exc:
            self.state.set_gui_action_message(f"Invalid first-stage hold time: {exc}")
            info(f"Invalid first-stage hold time from GUI: {exc}", Fore.RED)

    def apply_second_stage_hold_from_gui(self, event=None) -> None:
        try:
            hold_min = self._parse_hold_minutes_textbox("textbox_second_hold")
            self.state.set_second_stage_hold_s(hold_min * 60.0)
            self.state.set_gui_action_message(f"Second-stage hold time updated to {hold_min:.1f} min")
        except Exception as exc:
            self.state.set_gui_action_message(f"Invalid second-stage hold time: {exc}")
            info(f"Invalid second-stage hold time from GUI: {exc}", Fore.RED)

    def request_abort_from_gui(self, event=None) -> None:
        message = (
            f"Abort requested from GUI button. Safe shutdown will send oven PID SV to "
            f"{COOLDOWN_TARGET_C:.0f} °C and switch Keysight OFF."
        )
        banner(message)
        self.state.mark_abort(message)
        self.state.set_phase("ABORTING", message)
        self.state.stop_event.set()

    def start_threads(self) -> None:
        self.threads = [
            threading.Thread(target=monitor_oven, args=(self.pid, self.logger, self.state), daemon=True),
            threading.Thread(target=monitor_pyrometer, args=(self.state,), daemon=True),
            threading.Thread(target=monitor_ck1_temperature, args=(self.state,), daemon=True),
            threading.Thread(target=monitor_keysight, args=(self.keysight, self.state), daemon=True),
            threading.Thread(target=rampdown_keysight, args=(self.keysight, self.state), daemon=True),
            threading.Thread(target=run_annealing_sequence, args=(self.pid, self.state), daemon=True),
            threading.Thread(target=user_command_listener, args=(self.state,), daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def run(self) -> None:
        banner(
            "Starting NPG annealings controller.\n"
            "The evaporator current rampdown and the annealing sequence will run in parallel.\n"
            "After the rampdown reaches zero, the interface will ask you to switch off the evaporator\n"
            "and that warning will disappear automatically once the current and voltage signals are gone.\n\n"
            "Commands while running: 'i' + Enter = status, 'q' + Enter = stop"
        )

        try:
            self.keysight.configure_for_remote_rampdown()
            self.preflight_initial_current_a = self.keysight.measure_current_a()
        except Exception:
            self.preflight_initial_current_a = None

        if self.preflight_initial_current_a is None:
            self.preflight_initial_current_a = ask_positive_float(
                "Could not read the Keysight current automatically before the run. Enter the current in A to start the rampdown: "
            )
        self.state.initial_keysight_current_a = max(0.0, self.preflight_initial_current_a)
        self.state.last_keysight_set_current_a = self.state.initial_keysight_current_a

        self.start_threads()
        plt.ion()
        show_live_interface(self.fig)
        try:
            last_gui = 0.0
            last_flush = 0.0
            while not self.state.stop_event.is_set():
                now = time.time()
                if now - last_gui >= GUI_REFRESH_S:
                    update_figure(self.fig, self.artists, self.state)
                    last_gui = now
                if now - last_flush >= DATA_FLUSH_S:
                    ts = datetime.now()
                    latest_oven = latest_oven_temperature_c(self.state)
                    with self.state.data_lock:
                        latest_current = self.state.keysight_current_values[-1] if self.state.keysight_current_values else None
                        latest_voltage = self.state.keysight_voltage_values[-1] if self.state.keysight_voltage_values else None
                        latest_ck1 = self.state.ck1_temperature_values[-1] if self.state.ck1_temperature_values else None
                        latest_pyro = self.state.pyrometer_temperature_values[-1] if self.state.pyrometer_temperature_values else None
                        latest_sample = self.state.sample_temperature_estimates[-1] if self.state.sample_temperature_estimates else None
                        latest_pyro_status = self.state.pyrometer_status_values[-1] if self.state.pyrometer_status_values else self.state.pyrometer_status
                    self.logger.log_row(
                        timestamp=ts,
                        phase=self.state.phase,
                        oven_temperature_c=latest_oven,
                        oven_setpoint_c=self.state.oven_setpoint_c,
                        keysight_current_a=latest_current,
                        keysight_voltage_v=latest_voltage,
                        message=self.state.last_message,
                        ck1_temperature_c=latest_ck1,
                        raw_pyrometer_c=latest_pyro,
                        estimated_sample_c=latest_sample,
                        pyrometer_status=latest_pyro_status,
                    )
                    last_flush = now
                safe_gui_refresh(self.fig, 0.05)
                if self.state.finished_event.is_set() and self.state.phase == "FINISHED":
                    # Finalization is time-defined: 10 min after the 0 °C setpoint,
                    # save the final data/plots and close automatically. Physical
                    # evaporator power-off confirmation is still displayed when
                    # relevant, but it no longer keeps the acquisition running for hours.
                    update_figure(self.fig, self.artists, self.state)
                    self.state.stop_event.set()
        except KeyboardInterrupt:
            banner("KeyboardInterrupt received. Stopping the run.")
            _install_shutdown_signal_ignore_mode()
            self.state.mark_abort("KeyboardInterrupt received. Run aborted by user.")
            self.state.stop_event.set()
        finally:
            self.shutdown()

    def perform_abort_hardware_shutdown(self) -> None:
        if self.state.safe_shutdown_completed:
            return

        self.state.safe_shutdown_completed = True
        _install_shutdown_signal_ignore_mode()
        banner(
            f"Abort detected. Sending oven PID setpoint to {COOLDOWN_TARGET_C:.0f} °C "
            "and switching off the Keysight output."
        )

        try:
            self.pid.set_setpoint_c_best_effort(COOLDOWN_TARGET_C)
            self.state.oven_setpoint_c = COOLDOWN_TARGET_C
            info(
                f"Abort safety action: Oven PID setpoint sent to {COOLDOWN_TARGET_C:.1f} °C.",
                Fore.MAGENTA,
            )
        except BaseException as exc:
            info(
                f"Abort safety action failed while sending {COOLDOWN_TARGET_C:.1f} °C "
                f"to the oven PID: {exc}",
                Fore.RED,
            )

        try:
            self.keysight.shutdown_output()
            self.state.clear_switch_off_prompt("Abort safety action: Keysight output switched off.")
            info("Abort safety action: Keysight output switched off and communication closed next.", Fore.YELLOW)
        except BaseException as exc:
            info(f"Abort safety action failed while switching off the Keysight output: {exc}", Fore.RED)

    def shutdown(self) -> None:
        for thread in self.threads:
            if thread.is_alive():
                try:
                    thread.join(timeout=2.0)
                except Exception:
                    pass

        if self.state.abort_requested or (self.state.stop_event.is_set() and not self.state.anneal_finished_normally):
            self.perform_abort_hardware_shutdown()

        update_figure(self.fig, self.artists, self.state)
        try:
            save_pid_temperature_curve(self.pid_temperature_curve_path, self.state, self.run_name)
            info(f"Saved temperature comparison: {self.pid_temperature_curve_path}", Fore.CYAN)
        except Exception as exc:
            info(f"Could not save PID temperature curve: {exc}", Fore.RED)

        try:
            self.fig.savefig(self.final_plot_path, dpi=150, bbox_inches="tight")
            info(f"Saved final plot: {self.final_plot_path}", Fore.CYAN)
        except Exception as exc:
            info(f"Could not save final plot: {exc}", Fore.RED)

        try:
            save_run_summary(self.summary_path, self.state)
            info(f"Saved summary: {self.summary_path}", Fore.CYAN)
        except Exception as exc:
            info(f"Could not save summary: {exc}", Fore.RED)

        if self.state.anneal_finished_normally and not self.state.keysight_output_off_at_zero:
            try:
                self.keysight.shutdown_output()
                self.state.keysight_output_off_at_zero = True
                info("Finalization safety check: Keysight output confirmed OFF.", Fore.YELLOW)
            except Exception as exc:
                info(f"Finalization warning: could not confirm Keysight output OFF: {exc}", Fore.RED)

        self.logger.close()
        self.pid.close()
        self.keysight.close()

        # Serial release pause after final device close. This only helps Windows free
        # COM handles cleanly before another phase or diagnostic tool opens them.
        time.sleep(0.5)

        info(f"All data saved in: {self.output_dir}", Fore.CYAN)
        try:
            if AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED:
                plt.close('all')
            else:
                plt.ioff()
                plt.show()
        except Exception:
            pass


_APP: Optional[App] = None
_SHUTDOWN_SIGNAL_IGNORE_INSTALLED = False


def _install_shutdown_signal_ignore_mode() -> None:
    global _SHUTDOWN_SIGNAL_IGNORE_INSTALLED
    if _SHUTDOWN_SIGNAL_IGNORE_INSTALLED:
        return

    for _sig_name in ("SIGINT", "SIGTERM"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is None:
            continue
        try:
            signal.signal(_sig, signal.SIG_IGN)
        except Exception:
            pass

    _SHUTDOWN_SIGNAL_IGNORE_INSTALLED = True


def _failsafe_on_exit() -> None:
    global _APP
    if _APP is not None:
        try:
            _install_shutdown_signal_ignore_mode()
            if not _APP.state.anneal_finished_normally and not _APP.state.safe_shutdown_completed:
                _APP.state.mark_abort("Process exiting before normal completion.")
                _APP.state.stop_event.set()
                _APP.perform_abort_hardware_shutdown()
        except BaseException:
            pass


def _sigint_handler(signum, frame) -> None:
    global _APP
    if _APP is not None:
        _APP.state.mark_abort(f"Abort signal received ({signum}).")
        _APP.state.stop_event.set()
    raise KeyboardInterrupt


def main() -> None:
    global _APP
    colorama_init(autoreset=True)
    run_name = os.environ.get("NPG_CHAMBER_RUN_NAME", "").strip() or ask_nonempty_text(RUN_NAME_PROMPT)
    _APP = App(run_name)
    _APP.run()


try:
    signal.signal(signal.SIGINT, _sigint_handler)
except Exception:
    pass

try:
    signal.signal(signal.SIGTERM, _sigint_handler)
except Exception:
    pass

atexit.register(_failsafe_on_exit)

if __name__ == "__main__":
    main()
