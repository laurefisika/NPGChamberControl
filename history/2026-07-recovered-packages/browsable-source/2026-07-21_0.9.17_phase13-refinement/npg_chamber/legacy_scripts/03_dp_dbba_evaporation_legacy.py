import threading
import csv
import json
import serial
import time
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.widgets import TextBox, Button
from colorama import Fore, Style, init
import os
import re
import math
import atexit
import signal

from npg_chamber.config.run_parameters import (
    apply_overrides_to_namespace,
    format_override_summary,
    format_pyrometer_summary,
    load_phase_overrides,
    load_pyrometer_settings,
    write_effective_parameters,
)
from npg_chamber.devices.pyrometer import ImpacIPE140, PyrometerProfile, PyrometerSerialConfig
from npg_chamber.common.phase_dashboard_style import (
    AXIS_ACCENTS,
    add_panel_card,
    create_phase_badge,
    style_measurement_axis,
    update_phase_badge,
)

RUN_AUTOMATION_OVERRIDES = load_phase_overrides("dpdbba")

PYROMETER_SETTINGS = load_pyrometer_settings()
PYROMETER_PROFILE = PyrometerProfile(**PYROMETER_SETTINGS)
PYROMETER_SERIAL_CONFIG = PyrometerSerialConfig(port="COM10", baudrate=38400, address="00")
print("\n" + format_pyrometer_summary(PYROMETER_SETTINGS) + "\n")


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

def ask_nonempty_text(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Please enter a non-empty value.")


def ask_positive_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip().replace(',', '.')
        try:
            value = float(raw)
            if value > 0:
                return value
        except Exception:
            pass
        print("Please enter a positive number.")


run_name = os.environ.get("NPG_CHAMBER_RUN_NAME", "").strip() or ask_nonempty_text("Enter the name for this DP-DBBA evaporation run: ")

_env_ratio = os.environ.get("NPG_CHAMBER_THICKNESS_RATIO", "").strip().replace(',', '.')
try:
    input_thickness_ratio = float(_env_ratio) if _env_ratio else None
except Exception:
    input_thickness_ratio = None
if input_thickness_ratio is None or input_thickness_ratio <= 0:
    input_thickness_ratio = ask_positive_float("Enter the thickness ratio obtained in Heat up + Calibration: ")
AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED = os.environ.get("NPG_CHAMBER_UNIFIED_LAUNCHER", "").strip() == "1"

# DP-DBBA target thickness calculated from the Heat up + Calibration ratio.
# The ratio saved by script 1 is: CK-1 relative thickness / Sample relative thickness.
# Therefore, to obtain the desired sample-equivalent DP-DBBA thickness, the CK-1
# QMB target must be:
#     target_CK1 = ratio * target_sample_equivalent
DP_DBBA_SAMPLE_EQUIVALENT_THICKNESS_A = RUN_AUTOMATION_OVERRIDES.get("DP_DBBA_SAMPLE_EQUIVALENT_THICKNESS_A", 623.13 / 94.39)
IDEAL_CK1_EVAPORATION_THICKNESS_A = input_thickness_ratio * DP_DBBA_SAMPLE_EQUIVALENT_THICKNESS_A
REAL_SAMPLE_THICKNESS_A = IDEAL_CK1_EVAPORATION_THICKNESS_A / input_thickness_ratio
TARGET_CK1_THICKNESS_A = IDEAL_CK1_EVAPORATION_THICKNESS_A
EVAPORATION_TARGET_CK1_A = TARGET_CK1_THICKNESS_A
sample_name = run_name

# When script 3 starts, set the
# external oven PID setpoint to 200 ºC, then keep monitoring its PV.
OVEN_TARGET_TEMPERATURE_C = RUN_AUTOMATION_OVERRIDES.get("OVEN_TARGET_TEMPERATURE_C", 200.0)

# _____________________SAVE THE DATA____________________________________________
# Save data next to this script, not in the terminal's current working directory.
script_folder = _resolve_phase_data_parent("DP-DBBA Evaporation Data")
safe_sample_name = re.sub(r'[<>:"/\\|?*]+', '_', sample_name).strip() or 'unnamed_trial'
custom_folder_name = f"DP-DBBA Evaporation data {safe_sample_name}"
final_folder_path = os.path.join(script_folder, custom_folder_name)
os.makedirs(final_folder_path, exist_ok=True)

try:
    pyrometer_profile_path = os.path.join(final_folder_path, f"{safe_sample_name}_pyrometer_profile.json")
    with open(pyrometer_profile_path, "w", encoding="utf-8") as pyrometer_profile_file:
        json.dump(PYROMETER_SETTINGS, pyrometer_profile_file, indent=2, sort_keys=True)
        pyrometer_profile_file.write("\n")
    print(f"Saved pyrometer profile: {pyrometer_profile_path}")
except Exception as exc:
    print(f"Could not save pyrometer profile: {exc}")

pyrometer_csv_path = os.path.join(final_folder_path, f"{safe_sample_name}_pyrometer_temperatures.csv")
try:
    with open(pyrometer_csv_path, "w", newline="", encoding="utf-8") as pyrometer_csv_file:
        csv.writer(pyrometer_csv_file).writerow(
            [
                "timestamp",
                "raw_pyrometer_c",
                "estimated_sample_c",
                "status",
                "profile_name",
                "emissivity_percent",
            ]
        )
except Exception as exc:
    print(f"Could not initialize pyrometer CSV: {exc}")


# _____________________WHAT CHANGED IN THIS VERSION________________________
WHAT_CHANGED_TEXT = """WHAT CHANGED IN THIS DP-DBBA VERSION
============================================================
This build makes the DP-DBBA evaporation script follow the same control style as
1. Heat up + Calibration_NEW TRY_v7.3 while preserving the DP-DBBA-specific logic.
It also adds monitoring-only IMPAC IPE 140 profile, logging and temperature-view
support without changing evaporation, PID, Keysight or safety decisions.

Inherited from Heat up + Calibration v7.3:
1. Same live GUI style: editable CK-1 temperature target, CK-1 rate target,
   PID band, ramp-up mode, steps threshold, step period, and slope targets.
2. Same CK-1 temperature PID hold. Once the CK-1 reaches the editable target
   band, the Keysight current is regulated around the live temperature target.
3. Same independent temperature watchdog above the PID: soft action at
   target + 5 ºC and hard stop at target + 10 ºC, with sensor freshness and
   plausibility checks, with the same timing constants as script 1 v7.3.
4. Same Keysight safety concept: normal soft current cap, separate software
   hard current/voltage stops, separate Keysight OCP/OVP latch thresholds,
   output-off/protection-latch diagnostics, and voltage compliance guard.
5. The heating-to-open-shutter transition uses live editable targets and does
   not block when the CK-1 rate is above the upper band.
6. When editable targets are changed during a run, the internal control logic
   uses the new values immediately: heating condition, PID, watchdog, live
   reference lines, and ramp settings all read from the live dictionaries.

DP-DBBA-specific behaviour kept:
1. The script asks for the thickness ratio from Heat up + Calibration.
2. It calculates the DP-DBBA CK-1 target thickness as:
      target_CK1 = thickness_ratio * (623.13 / 94.39)
3. The post-shutter phase is EVAPORATION, not CALIBRATION.
4. On Open Shutter confirmation, the QMB evaporation window is reset so CK-1
   and Sample relative thickness start from zero.
5. The finish criterion is CK-1 relative thickness reaching the calculated
   DP-DBBA target, not Sample thickness reaching 1 Å.
6. After target thickness is reached, the script waits for Close Shutter
   confirmation before handing off to the NPG Annealings script; normal completion sets the Keysight to base current and performs no ramp-down.
7. At startup, the external Oven PID setpoint is explicitly written to 200 ºC,
   preserving the original DP-DBBA script behaviour.
8. The GUI includes the Manual Keysight current block from Heat up + Calibration:
   Set manual I pauses automated Steps/Slope/PID current corrections while keeping
   watchdog and safety protections active; Back to Auto resumes automated control.

Default DP-DBBA working values in this version:
- KEYSIGHT_START_CURRENT_A = 0.005
- KEYSIGHT_BASE_WORK_CURRENT_A = 0.640
- KEYSIGHT_SOFT_WARNING_A = 0.670
- KEYSIGHT_HARD_STOP_A = 0.685
- KEYSIGHT_INSTRUMENT_OCP_MARGIN_A = 0.005
- KEYSIGHT_INSTRUMENT_OCP_A = KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A
- KEYSIGHT_VOLTAGE_LIMIT_V = 2.30
- KEYSIGHT_HARD_STOP_V = 2.45
- KEYSIGHT_INSTRUMENT_OVP_MARGIN_V = 0.05
- KEYSIGHT_INSTRUMENT_OVP_V = KEYSIGHT_HARD_STOP_V + KEYSIGHT_INSTRUMENT_OVP_MARGIN_V
- HEATING_TRIGGER_TEMP_C = 242.0
- CK1_RATE_TARGET_A_PER_S = 0.40
- CK1_RATE_AVG_WINDOW_POINTS = 8
- PID_TEMP_BAND_C = 0.75
- TEMP_WATCHDOG_SOFT_MARGIN_C = 5.0
- TEMP_WATCHDOG_HARD_MARGIN_C = 10.0
"""

def show_what_changed():
    print("\n" + WHAT_CHANGED_TEXT + "\n")


show_what_changed()


init()
stop_event = threading.Event()
data_lock = threading.Lock()
keysight_lock = threading.Lock()
state_lock = threading.Lock()
pid_lock = threading.Lock()
oven_pid_state = {
    'setpoint_c': None,
    'last_confirmed_pv_c': None,
}

# _____________________AUTOMATION PARAMETERS____________________________________
AUTO_KEYSIGHT_ENABLED = True

# Start from a small non-zero current and ramp upward under software control.
KEYSIGHT_START_CURRENT_A = 0.005
KEYSIGHT_BASE_WORK_CURRENT_A = 0.640

# Normal operation cap: the automation must never command above this value.
KEYSIGHT_SOFT_WARNING_A = 0.670

# Software hard current safety value. The script stops if measured current reaches this.
KEYSIGHT_HARD_STOP_A = 0.685

# Hardware latch protection in the Keysight. Keep this above the software hard stop
# to avoid nuisance OCP trips from short transients/readback tolerances while the
# script is intentionally operating near the 0.670 A soft cap.
KEYSIGHT_INSTRUMENT_OCP_MARGIN_A = 0.005
KEYSIGHT_INSTRUMENT_OCP_A = KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A

KEYSIGHT_STEP_A = 0.005
KEYSIGHT_STEP_PERIOD_S = 15.0

# Voltage limit = normal compliance limit. Hard stop = emergency threshold.
KEYSIGHT_VOLTAGE_LIMIT_V = 2.30
KEYSIGHT_RANGE = 'LOW'            # 15V / 7A
KEYSIGHT_HARD_STOP_V = 2.45

# Hardware OVP latch in the Keysight. Software still stops at KEYSIGHT_HARD_STOP_V.
KEYSIGHT_INSTRUMENT_OVP_MARGIN_V = 0.05
KEYSIGHT_INSTRUMENT_OVP_V = KEYSIGHT_HARD_STOP_V + KEYSIGHT_INSTRUMENT_OVP_MARGIN_V
VOLTAGE_LIMIT_GUARD_COOLDOWN_S = 3.0
# PID temperature hold configuration.
# Ramp-up modes are still used to approach the target; PID takes over around
# the editable CK-1 temperature target.
PID_CONTROL_PERIOD_S = 8.0
PID_TEMP_BAND_C = 1.0
PID_KP_A_PER_C = 0.0020
PID_KI_A_PER_C_S = 0.000030
PID_KD_A_PER_C_PER_S = 0.0030
PID_MAX_STEP_A = 0.0025
PID_INTEGRAL_LIMIT_C_S = 250.0

# Independent temperature watchdog.
# This is intentionally separate from the PID: if the PID misbehaves, stops
# correcting, or the CK-1 sensor goes stale/unphysical, this layer can still
# force a current reduction or shut the Keysight output off.
TEMP_WATCHDOG_ENABLED = True
TEMP_WATCHDOG_PERIOD_S = 5.0
TEMP_WATCHDOG_SOFT_MARGIN_C = 5.0       # target + this value => force current down
TEMP_WATCHDOG_HARD_MARGIN_C = 10.0      # target + this value => output OFF / SAFETY_STOP
TEMP_WATCHDOG_SOFT_STEP_A = 0.010       # forced current reduction per soft action
TEMP_WATCHDOG_SOFT_COOLDOWN_S = 0.50    # forced current reduction per hard action
TEMP_WATCHDOG_SENSOR_STALE_TIMEOUT_S = 180.0
TEMP_WATCHDOG_SENSOR_INITIAL_GRACE_S = 120.0
TEMP_WATCHDOG_VALID_MIN_C = -20.0
TEMP_WATCHDOG_VALID_MAX_C = 500.0
TEMP_WATCHDOG_MAX_JUMP_C = 35.0         # reject sudden impossible jumps between reads
TEMP_WATCHDOG_ACTIVE_PHASES = ('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'EVAPORATION', 'WAIT_SHUTTER_CLOSE')

# Ramp-up mode configuration. The live GUI can edit the mode, threshold and
# period while the run is active.
RAMP_MODE_STEPS = 'steps'
RAMP_MODE_SLOPE = 'slope'
DEFAULT_RAMP_UP_MODE = RAMP_MODE_STEPS
STEPS_RAMP_UNTIL_TEMP_C = 100.0
STEPS_RAMP_STEP_PERIOD_S = 15.0

HEATING_TRIGGER_TEMP_C = 242.0
CK1_RATE_TARGET_A_PER_S = 0.40
CK1_RATE_LOW_A_PER_S = CK1_RATE_TARGET_A_PER_S -  0.05
CK1_RATE_HIGH_A_PER_S = CK1_RATE_TARGET_A_PER_S +  0.05
CK1_RATE_AVG_WINDOW_POINTS = 8

# Temperature-slope-based ramp control for the initial heating phase.
# The goal is not to make current linear in time, but to keep temperature
# approximately linear in time while approaching the working current safely.
TEMP_SLOPE_WINDOW_POINTS = 15
TEMP_SLOPE_TARGET_EARLY_C_PER_MIN = 9.0
TEMP_SLOPE_TARGET_MID_C_PER_MIN = 8.0
TEMP_SLOPE_TARGET_LATE_C_PER_MIN = 7.0
TEMP_SLOPE_DEADBAND_C_PER_MIN = 0.20
TEMP_SLOPE_KP_A_PER_C_PER_MIN = 0.010
FAST_RAMP_CURRENT_THRESHOLD_A = 0.50
MID_RAMP_CURRENT_THRESHOLD_A = 0.60
EARLY_RAMP_MAX_STEP_A = 0.005
MID_RAMP_MAX_STEP_A = 0.005
LATE_RAMP_MAX_STEP_A = 0.005


# Abort-button ramp-down. Normal DP-DBBA completion does not use this ramp-down;
# it leaves the Keysight output ON at KEYSIGHT_BASE_WORK_CURRENT_A for script 4.
RAMPDOWN_STEP_A = 0.010
RAMPDOWN_STEP_PERIOD_S = 15
RAMPDOWN_ZERO_THRESHOLD_A = 0.003

# Threshold used to decide whether a non-ramp phase still expects Keysight output ON.
ZERO_CURRENT_THRESHOLD_A = RAMPDOWN_ZERO_THRESHOLD_A

# Apply validated launcher values only inside this child process. The target
# thickness and oven target above were read early because they are needed before
# the rest of the automation constants are created.
apply_overrides_to_namespace("dpdbba", globals(), RUN_AUTOMATION_OVERRIDES)
KEYSIGHT_INSTRUMENT_OCP_A = KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A
KEYSIGHT_INSTRUMENT_OVP_V = KEYSIGHT_HARD_STOP_V + KEYSIGHT_INSTRUMENT_OVP_MARGIN_V
CK1_RATE_LOW_A_PER_S = CK1_RATE_TARGET_A_PER_S - 0.05
CK1_RATE_HIGH_A_PER_S = CK1_RATE_TARGET_A_PER_S + 0.05
ZERO_CURRENT_THRESHOLD_A = RAMPDOWN_ZERO_THRESHOLD_A
print("\n" + format_override_summary("dpdbba", RUN_AUTOMATION_OVERRIDES) + "\n")
try:
    parameter_record_path = write_effective_parameters(
        os.path.join(final_folder_path, f"{safe_sample_name}_automation_parameters.json"),
        "dpdbba",
        RUN_AUTOMATION_OVERRIDES,
    )
    print(f"Saved effective automation parameters: {parameter_record_path}")
except Exception as exc:
    print(f"Could not save effective automation parameters: {exc}")

# Shared Keysight state
keysight_state = {
    'automation_enabled': AUTO_KEYSIGHT_ENABLED,
    'automation_active': False,
    'set_current_a': None,
    'hold_current_a': None,
    'set_voltage_limit_v': KEYSIGHT_VOLTAGE_LIMIT_V,
    'last_step_at': None,
    'reason_stopped': None,

    'last_soft_cap_warning_at': 0.0,
    'last_voltage_limit_guard_at': 0.0,
}

temperature_pid_state = {
    'integral_error_c_s': 0.0,
    'last_error_c': None,
    'last_time': None,
    'last_log_at': 0.0,
}

temperature_watchdog_state = {
    'last_temp_c': None,
    'last_temp_timestamp': None,
    'last_soft_action_at': 0.0,
    'last_log_at': 0.0,
    'hard_stop_triggered': False,
}


# Shared process state
process_state = {
    'phase': 'HEATING_UP',
    'phase_started_at': time.time(),
    'transition_reason': None,
    'baseline_ck1_thickness': None,
    'baseline_sample_thickness': None,
    'shutter_open_confirmed': False,
    'shutter_close_confirmed': False,
    'last_status_print': 0.0,
    'snapshot_taken': False,
    'final_snapshot_taken': False,
    'plots_reset_for_evaporation': False,
    # Normal DP-DBBA completion should leave the Keysight ON at base current for script 4.
    'normal_completion': False,
    'leave_keysight_on_for_next_script': False,
    'automation_active_at_finish': None,
    'keysight_current_at_finish_a': None,
    'keysight_measured_current_at_finish_a': None,
    'keysight_voltage_at_finish_v': None,
}


heating_targets_lock = threading.Lock()
live_heating_targets = {
    'trigger_temp_c': HEATING_TRIGGER_TEMP_C,
    'rate_target_a_per_s': CK1_RATE_TARGET_A_PER_S,
    'rate_low_a_per_s': CK1_RATE_LOW_A_PER_S,
    'rate_high_a_per_s': CK1_RATE_HIGH_A_PER_S,
    'pid_temp_band_c': PID_TEMP_BAND_C,
}
DEFAULT_LIVE_HEATING_TARGETS = dict(live_heating_targets)

ramp_settings_lock = threading.Lock()
live_ramp_settings = {
    'mode': DEFAULT_RAMP_UP_MODE,
    'steps_until_temp_c': STEPS_RAMP_UNTIL_TEMP_C,
    'steps_step_period_s': STEPS_RAMP_STEP_PERIOD_S,
    'slope_early_c_per_min': TEMP_SLOPE_TARGET_EARLY_C_PER_MIN,
    'slope_mid_c_per_min': TEMP_SLOPE_TARGET_MID_C_PER_MIN,
    'slope_late_c_per_min': TEMP_SLOPE_TARGET_LATE_C_PER_MIN,
}
DEFAULT_LIVE_RAMP_SETTINGS = dict(live_ramp_settings)

def build_run_info_text():
    return (
        f"run_name: {run_name}\n"
        f"input_thickness_ratio: {input_thickness_ratio:.6f}\n"
        f"ideal_ck1_evaporation_thickness_a: {IDEAL_CK1_EVAPORATION_THICKNESS_A:.6f}\n"
        f"real_sample_thickness_a: {REAL_SAMPLE_THICKNESS_A:.6f}\n"
        f"target_ck1_thickness_a: {TARGET_CK1_THICKNESS_A:.6f}\n"
        f"oven_pid_target_temperature_c: {OVEN_TARGET_TEMPERATURE_C:.1f}\n"
        f"ready_condition_temperature_c_initial: >= {HEATING_TRIGGER_TEMP_C:.1f}\n"
        f"ready_condition_avg_rate_a_per_s_initial: >= {CK1_RATE_TARGET_A_PER_S:.3f}\n"
        f"ready_condition_points: {CK1_RATE_AVG_WINDOW_POINTS}\n"
        f"pid_temp_band_c_initial: {PID_TEMP_BAND_C:.3f}\n"
        f"watchdog_soft_margin_c: {TEMP_WATCHDOG_SOFT_MARGIN_C:.3f}\n"
        f"watchdog_hard_margin_c: {TEMP_WATCHDOG_HARD_MARGIN_C:.3f}\n"
    )

def save_run_parameters():
    path = os.path.join(final_folder_path, f"{sample_name}_run_parameters.txt")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(build_run_info_text())
        print(f"Saved run parameters: {path}")
    except Exception as e:
        print(f"Could not save run parameters: {e}")


print(build_run_info_text())
save_run_parameters()

GUI_REFRESH_INTERVAL_S = 0.15
DATA_SAVE_INTERVAL_S = 5.0
MAX_PLOT_POINTS_PER_SERIES = 1200
AUTOSCALE_EVERY_N_REFRESHES = 4
# _____________________DEVICE CONFIG____________________________________________
device_info = {
    'CK-1 evaporator QMB': {'port': 'COM4', 'baud_rate': 115200},
    'Sample QMB': {'port': 'COM16', 'baud_rate': 115200},
    'XGS600 HFIG pressure': {'port': 'COM6', 'baud_rate': 9600},
    'Oven PID temperature': {'port': 'COM9', 'baud_rate': 9600},
    'Keysight power supply': {'port': 'COM17', 'baud_rate': 9600},
    'Arduino CK-1 crucible temperature': {'port': 'COM3', 'baud_rate': 9600},
}
QMBs = {'CK-1 evaporator QMB', 'Sample QMB'}
timeout = 1

data = {
    'CK-1 evaporator QMB': {'thickness_times': [], 'rate_times': [], 'thickness_data': [], 'rate_data': []},
    'Sample QMB': {'thickness_times': [], 'rate_times': [], 'thickness_data': [], 'rate_data': []},
    'XGS600 HFIG pressure': {'pressure_times': [], 'pressure_data': []},
    'Oven PID temperature': {'temperature_times': [], 'temperature_data': []},
    'IMPAC pyrometer': {
        'temperature_times': [],
        'temperature_data': [],
        'sample_temperature_data': [],
        'status_data': [],
    },
    'Keysight power supply': {'current_times': [], 'current_data': [], 'voltage_times': [], 'voltage_data': []},
    'Arduino CK-1 crucible temperature': {'temperature_times': [], 'temperature_data': []},
}


# QMB thickness offset handling. Both QMBs are zeroed at script start by the
# hardware command. At shutter opening, offsets are updated and the QMB data
# lists are cleared so the DP-DBBA evaporation plots start from 0 Å.
qmb_thickness_offsets = {key: 0.0 for key in QMBs}
raw_qmb_last_values = {
    key: {'thickness': None, 'rate': None}
    for key in QMBs
}

QMB__bytes = {
    "STX": b'\x02',
    "ADDR": b'\x10',
    "CMD_RSP": b'\x80',
    "CR": b'\x0D',
}
QMB__sub_commands = {
    'thickness': b'S',
    'rate': b'T',
    'zero': b'B',
}

def QMB__calculate_checksum(command):
    checksum = sum(command) % 256
    upper_nibble = (checksum >> 4) & 0x0F
    lower_nibble = checksum & 0x0F
    return bytes([upper_nibble + 0x30, lower_nibble + 0x30])

QMB__commands = {
    'thickness': QMB__bytes['STX'] + QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['thickness'] + QMB__calculate_checksum(QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['thickness']) + QMB__bytes['CR'],
    'rate': QMB__bytes['STX'] + QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['rate'] + QMB__calculate_checksum(QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['rate']) + QMB__bytes['CR'],
    'zero': QMB__bytes['STX'] + QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['zero'] + QMB__calculate_checksum(QMB__bytes['ADDR'] + QMB__bytes['CMD_RSP'] + QMB__sub_commands['zero']) + QMB__bytes['CR'],
}

# _____________________PLOTS____________________________________________________
fig, ((ax_thickness_ck1, ax_rate_ck1, ax_pressure_xgs600),
      (ax_thickness_sample, ax_rate_sample, ax_temperature_oven),
      (ax_current_keysight, ax_voltage_keysight, ax_temperature_ck1)) = plt.subplots(3, 3, figsize=(19.2, 11.7))
fig.patch.set_facecolor('#f4f6f8')
# A larger vertical gap gives every graph title its own header space and keeps
# the temperature selector clear of the plots above and below it.
plt.subplots_adjust(left=0.055, right=0.742, top=0.865, bottom=0.075, hspace=0.60, wspace=0.30)
plt.ion()

line_thickness_ck1, = ax_thickness_ck1.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['ck1'])
line_rate_ck1, = ax_rate_ck1.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['ck1'])
line_thickness_sample, = ax_thickness_sample.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['sample'])
line_rate_sample, = ax_rate_sample.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['sample'])
line_pressure_xgs600, = ax_pressure_xgs600.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['pressure'])
line_temperature_oven, = ax_temperature_oven.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['temperature'])
line_current_keysight, = ax_current_keysight.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['current'])
line_voltage_keysight, = ax_voltage_keysight.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['voltage'])
line_temperature_ck1, = ax_temperature_ck1.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['ck1_temperature'])

AXIS_INFO_STYLE = dict(
    boxstyle='round,pad=0.28',
    facecolor='#f8fafc',
    edgecolor='#d6dee8',
    linewidth=0.9,
    alpha=0.96,
)

plot_axes_config = [
    (ax_thickness_ck1, 'CK-1 QMB thickness', 'Thickness (Å)', AXIS_ACCENTS['ck1']),
    (ax_rate_ck1, 'CK-1 QMB rate', 'Rate (Å/s)', AXIS_ACCENTS['ck1']),
    (ax_thickness_sample, 'Sample QMB thickness', 'Thickness (Å)', AXIS_ACCENTS['sample']),
    (ax_rate_sample, 'Sample QMB rate', 'Rate (Å/s)', AXIS_ACCENTS['sample']),
    (ax_pressure_xgs600, 'Chamber pressure', 'Pressure (mbar)', AXIS_ACCENTS['pressure']),
    (ax_temperature_oven, 'Oven PID temperature', 'Temperature (ºC)', AXIS_ACCENTS['temperature']),
    (ax_current_keysight, 'Evaporator current', 'Current (A)', AXIS_ACCENTS['current']),
    (ax_voltage_keysight, 'Evaporator voltage', 'Voltage (V)', AXIS_ACCENTS['voltage']),
    (ax_temperature_ck1, 'CK-1 crucible temperature', 'Temperature (ºC)', AXIS_ACCENTS['ck1_temperature']),
]

for ax, title, ylabel, accent in plot_axes_config:
    style_measurement_axis(ax, title, ylabel, accent)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))

for ax in [
    ax_thickness_ck1,
    ax_rate_ck1,
    ax_pressure_xgs600,
    ax_thickness_sample,
    ax_rate_sample,
    ax_temperature_oven,
]:
    ax.set_xlabel('')

phase_title_text = create_phase_badge(fig, 'STARTING')

axis_info_texts = {
    'ck1_thickness': ax_thickness_ck1.text(0.02, 0.98, '', transform=ax_thickness_ck1.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'ck1_rate': ax_rate_ck1.text(0.02, 0.98, '', transform=ax_rate_ck1.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'sample_thickness': ax_thickness_sample.text(0.02, 0.98, '', transform=ax_thickness_sample.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'sample_rate': ax_rate_sample.text(0.02, 0.98, '', transform=ax_rate_sample.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'pressure': ax_pressure_xgs600.text(0.02, 0.98, '', transform=ax_pressure_xgs600.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'oven_temp': ax_temperature_oven.text(0.02, 0.98, '', transform=ax_temperature_oven.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'current': ax_current_keysight.text(0.02, 0.98, '', transform=ax_current_keysight.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'voltage': ax_voltage_keysight.text(0.02, 0.98, '', transform=ax_voltage_keysight.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
    'ck1_temp': ax_temperature_ck1.text(0.02, 0.98, '', transform=ax_temperature_ck1.transAxes, va='top', ha='left', fontsize=8.5, bbox=AXIS_INFO_STYLE),
}

# _____________________HELPERS__________________________________________________
LIVE_PLOT_ENABLED = True
LIVE_PLOT_FAILURE_REPORTED = False
pending_snapshots = []
pending_snapshots_lock = threading.Lock()
plot_refresh_counter = 0

pyrometer_reader = None
pyrometer_reader_lock = threading.Lock()
pyrometer_state = {
    'status': 'disabled' if not PYROMETER_PROFILE.enabled else 'waiting',
    'confirmed_emissivity_percent': None,
    'last_error': '',
}
temperature_view_state = {'mode': PYROMETER_PROFILE.default_view}
temperature_view_buttons = {}

live_target_temp_line = ax_temperature_ck1.axhline(HEATING_TRIGGER_TEMP_C, linestyle='--', linewidth=1.1, color='black', alpha=0.8)
live_target_rate_line = ax_rate_ck1.axhline(CK1_RATE_TARGET_A_PER_S, linestyle='--', linewidth=1.1, color='black', alpha=0.8)
live_target_rate_low_line = ax_rate_ck1.axhline(CK1_RATE_LOW_A_PER_S, linestyle=':', linewidth=1.0, color='gray', alpha=0.85)
live_target_rate_high_line = ax_rate_ck1.axhline(CK1_RATE_HIGH_A_PER_S, linestyle=':', linewidth=1.0, color='gray', alpha=0.85)

control_panel_ax = None
live_target_textboxes = {}
live_target_buttons = {}
live_ramp_textboxes = {}
live_ramp_buttons = {}
live_manual_textboxes = {}
live_manual_buttons = {}
live_target_status_text = None
live_dashboard_text = None

# Matplotlib/Tk artists must only be modified from the main GUI loop. Background
# threads store the latest status text here; update_live_plot() applies it safely.
live_action_status_lock = threading.Lock()
live_action_status_text = ''

manual_current_lock = threading.Lock()
manual_current_state = {
    # When enabled, the Keysight automation thread does not perform Steps ramp,
    # Slope ramp, or temperature PID current corrections. Safety checks and the
    # independent temperature watchdog remain active.
    'enabled': False,
    'requested_current_a': KEYSIGHT_START_CURRENT_A,
    'last_applied_current_a': None,
    'last_changed_at': None,
    'last_hold_log_at': 0.0,
}


def running_in_main_thread():
    return threading.current_thread() is threading.main_thread()


def get_live_heating_targets():
    with heating_targets_lock:
        return dict(live_heating_targets)


def get_heating_trigger_temp_c():
    return get_live_heating_targets()['trigger_temp_c']


def get_ck1_rate_target_a_per_s():
    return get_live_heating_targets()['rate_target_a_per_s']


def get_ck1_rate_low_a_per_s():
    return get_live_heating_targets()['rate_low_a_per_s']


def get_ck1_rate_high_a_per_s():
    return get_live_heating_targets()['rate_high_a_per_s']


def ck1_rate_band_from_target(rate_target_a_per_s):
    """Return the CK-1 rate acceptance band derived from the target rate.

    The GUI exposes only the target rate. The low/high values are dependent
    variables, matching script 1 v7.3: editing the target rate updates both
    band edges everywhere in the internal logic.
    """
    rate_target_a_per_s = float(rate_target_a_per_s)
    band_half_width = max(0.0, (CK1_RATE_HIGH_A_PER_S - CK1_RATE_LOW_A_PER_S) / 2.0)
    rate_low_a_per_s = max(0.0, rate_target_a_per_s - band_half_width)
    rate_high_a_per_s = rate_target_a_per_s + band_half_width
    return rate_low_a_per_s, rate_high_a_per_s


def get_pid_temp_band_c():
    return get_live_heating_targets()['pid_temp_band_c']


def refresh_live_target_lines():
    targets = get_live_heating_targets()
    live_target_temp_line.set_ydata([targets['trigger_temp_c'], targets['trigger_temp_c']])
    live_target_rate_line.set_ydata([targets['rate_target_a_per_s'], targets['rate_target_a_per_s']])
    live_target_rate_low_line.set_ydata([targets['rate_low_a_per_s'], targets['rate_low_a_per_s']])
    live_target_rate_high_line.set_ydata([targets['rate_high_a_per_s'], targets['rate_high_a_per_s']])



def set_live_heating_targets(trigger_temp_c, rate_target_a_per_s, *args):
    """Update live heat-up targets using the script-1 v7.3 dependent rate band.

    Accepts both the old DP-DBBA call style
        (trigger, rate_target, rate_low, rate_high, pid_band)
    and the v7.3 call style
        (trigger, rate_target, pid_band).
    Any supplied low/high values are ignored deliberately so the band always
    follows the editable target rate by formula.
    """
    if len(args) == 1:
        pid_temp_band_c = args[0]
    elif len(args) == 3:
        _old_rate_low_a_per_s, _old_rate_high_a_per_s, pid_temp_band_c = args
    else:
        raise TypeError(
            'set_live_heating_targets expects (trigger, rate_target, pid_band) '
            'or (trigger, rate_target, rate_low, rate_high, pid_band)'
        )

    rate_low_a_per_s, rate_high_a_per_s = ck1_rate_band_from_target(rate_target_a_per_s)
    pid_temp_band_c = max(0.1, float(pid_temp_band_c))

    with heating_targets_lock:
        live_heating_targets['trigger_temp_c'] = float(trigger_temp_c)
        live_heating_targets['rate_target_a_per_s'] = float(rate_target_a_per_s)
        live_heating_targets['rate_low_a_per_s'] = rate_low_a_per_s
        live_heating_targets['rate_high_a_per_s'] = rate_high_a_per_s
        live_heating_targets['pid_temp_band_c'] = pid_temp_band_c

    refresh_live_target_lines()

    message = (
        f"Live heating targets updated | T={float(trigger_temp_c):.1f} ºC | "
        f"rate target={float(rate_target_a_per_s):.3f} Å/s | "
        f"rate band=[{rate_low_a_per_s:.3f}, {rate_high_a_per_s:.3f}] Å/s | "
        f"PID T band=±{pid_temp_band_c:.2f} ºC"
    )

    print_banner(message)
    _set_live_action_status(message)

    if running_in_main_thread():
        try:
            fig.canvas.draw_idle()
        except Exception:
            pass

    reset_temperature_pid('Live temperature target or PID band changed')



def _apply_live_targets_from_widgets(event=None):
    try:
        trigger_temp_c = float(live_target_textboxes['trigger_temp_c'].text.strip())
        rate_target_a_per_s = float(live_target_textboxes['rate_target_a_per_s'].text.strip())
        pid_temp_band_c = float(live_target_textboxes['pid_temp_band_c'].text.strip())
    except Exception as e:
        message = f"Could not parse live target input: {e}"
        print(message)
        _set_live_action_status(message)
        if running_in_main_thread():
            try:
                fig.canvas.draw_idle()
            except Exception:
                pass
        return

    set_live_heating_targets(
        trigger_temp_c,
        rate_target_a_per_s,
        pid_temp_band_c,
    )



def _reset_live_targets_to_defaults(event=None):
    defaults = dict(DEFAULT_LIVE_HEATING_TARGETS)
    live_target_textboxes['trigger_temp_c'].set_val(f"{defaults['trigger_temp_c']:.1f}")
    live_target_textboxes['rate_target_a_per_s'].set_val(f"{defaults['rate_target_a_per_s']:.3f}")
    live_target_textboxes['pid_temp_band_c'].set_val(f"{defaults['pid_temp_band_c']:.2f}")

    set_live_heating_targets(
        defaults['trigger_temp_c'],
        defaults['rate_target_a_per_s'],
        defaults['pid_temp_band_c'],
    )


def get_live_ramp_settings():
    with ramp_settings_lock:
        return dict(live_ramp_settings)


def ramp_mode_label(mode=None):
    mode = mode or get_live_ramp_settings()['mode']
    if mode == RAMP_MODE_STEPS:
        return 'Steps ramp up mode'
    if mode == RAMP_MODE_SLOPE:
        return 'Slope ramp up mode'
    return str(mode)


def set_live_ramp_settings(
    mode=None,
    steps_until_temp_c=None,
    steps_step_period_s=None,
    slope_early_c_per_min=None,
    slope_mid_c_per_min=None,
    slope_late_c_per_min=None,
):
    with ramp_settings_lock:
        if mode is not None:
            if mode not in (RAMP_MODE_STEPS, RAMP_MODE_SLOPE):
                raise ValueError(f"Unknown ramp mode: {mode}")
            live_ramp_settings['mode'] = mode
        if steps_until_temp_c is not None:
            live_ramp_settings['steps_until_temp_c'] = max(0.0, float(steps_until_temp_c))
        if steps_step_period_s is not None:
            live_ramp_settings['steps_step_period_s'] = max(1.0, float(steps_step_period_s))
        if slope_early_c_per_min is not None:
            live_ramp_settings['slope_early_c_per_min'] = max(0.0, float(slope_early_c_per_min))
        if slope_mid_c_per_min is not None:
            live_ramp_settings['slope_mid_c_per_min'] = max(0.0, float(slope_mid_c_per_min))
        if slope_late_c_per_min is not None:
            live_ramp_settings['slope_late_c_per_min'] = max(0.0, float(slope_late_c_per_min))
        settings = dict(live_ramp_settings)

    if running_in_main_thread():
        refresh_ramp_mode_button_styles()

    message = (
        f"Ramp-up mode updated | {ramp_mode_label(settings['mode'])} | "
        f"steps until T={settings['steps_until_temp_c']:.1f} ºC | "
        f"step period={settings['steps_step_period_s']:.1f} s | "
        f"slopes E/M/L={settings['slope_early_c_per_min']:.2f}/"
        f"{settings['slope_mid_c_per_min']:.2f}/"
        f"{settings['slope_late_c_per_min']:.2f} ºC/min | "
        f"step size={KEYSIGHT_STEP_A:.3f} A"
    )
    print_banner(message)
    _set_live_action_status(message)
    if running_in_main_thread():
        try:
            fig.canvas.draw_idle()
        except Exception:
            pass


def refresh_ramp_mode_button_styles():
    if not live_ramp_buttons:
        return
    mode = get_live_ramp_settings()['mode']
    styles = {
        RAMP_MODE_STEPS: {
            'active': '#c7d2fe',
            'inactive': '#eef2ff',
            'text': '#312e81',
        },
        RAMP_MODE_SLOPE: {
            'active': '#bae6fd',
            'inactive': '#eff6ff',
            'text': '#075985',
        },
    }
    for button_key, button in live_ramp_buttons.items():
        if button_key not in (RAMP_MODE_STEPS, RAMP_MODE_SLOPE):
            continue
        is_active = (button_key == mode)
        style = styles[button_key]
        button.ax.set_facecolor(style['active'] if is_active else style['inactive'])
        button.label.set_color(style['text'])
        button.label.set_fontweight('bold' if is_active else 'normal')


def should_use_steps_ramp(current_temp_c):
    settings = get_live_ramp_settings()
    # If there is no temperature reading yet, fixed steps are safer and more
    # predictable than trying to estimate a slope with no data.
    if current_temp_c is None:
        return True

    # Fixed-step warm-up is used until the live editable threshold is reached.
    # After that threshold, the slope controller takes over automatically.
    return current_temp_c < settings['steps_until_temp_c']


def maybe_auto_switch_steps_to_slope(current_temp_c):
    if current_temp_c is None:
        return False

    settings = get_live_ramp_settings()
    if settings['mode'] != RAMP_MODE_STEPS:
        return False
    if current_temp_c < settings['steps_until_temp_c']:
        return False

    set_live_ramp_settings(mode=RAMP_MODE_SLOPE)

    message = (
        f"Automatic ramp transition: CK-1 temp {current_temp_c:.1f} ºC reached "
        f"the steps-ramp threshold {settings['steps_until_temp_c']:.1f} ºC. "
        f"Slope ramp is now active."
    )
    print_banner(message)
    _set_live_action_status(message.replace('. ', '.\n'))
    if running_in_main_thread():
        try:
            fig.canvas.draw_idle()
        except Exception:
            pass
    return True


def current_ramp_step_period_s(current_temp_c, current_setpoint_a):
    if current_setpoint_a < KEYSIGHT_BASE_WORK_CURRENT_A and should_use_steps_ramp(current_temp_c):
        return get_live_ramp_settings()['steps_step_period_s']
    return KEYSIGHT_STEP_PERIOD_S


def _apply_live_ramp_settings_from_widgets(event=None):
    try:
        steps_until_temp_c = float(live_ramp_textboxes['steps_until_temp_c'].text.strip())
        steps_step_period_s = float(live_ramp_textboxes['steps_step_period_s'].text.strip())
        slope_early_c_per_min = float(live_ramp_textboxes['slope_early_c_per_min'].text.strip())
        slope_mid_c_per_min = float(live_ramp_textboxes['slope_mid_c_per_min'].text.strip())
        slope_late_c_per_min = float(live_ramp_textboxes['slope_late_c_per_min'].text.strip())
    except Exception as e:
        message = f"Could not parse ramp-up input: {e}"
        print(message)
        if live_target_status_text is not None:
            live_target_status_text.set_text(message)
        try:
            fig.canvas.draw_idle()
        except Exception:
            pass
        return

    set_live_ramp_settings(
        steps_until_temp_c=steps_until_temp_c,
        steps_step_period_s=steps_step_period_s,
        slope_early_c_per_min=slope_early_c_per_min,
        slope_mid_c_per_min=slope_mid_c_per_min,
        slope_late_c_per_min=slope_late_c_per_min,
    )


def _set_steps_ramp_mode(event=None):
    set_live_ramp_settings(mode=RAMP_MODE_STEPS)


def _set_slope_ramp_mode(event=None):
    set_live_ramp_settings(mode=RAMP_MODE_SLOPE)


def _reset_live_ramp_settings_to_defaults(event=None):
    defaults = dict(DEFAULT_LIVE_RAMP_SETTINGS)
    if 'steps_until_temp_c' in live_ramp_textboxes:
        live_ramp_textboxes['steps_until_temp_c'].set_val(f"{defaults['steps_until_temp_c']:.1f}")
    if 'steps_step_period_s' in live_ramp_textboxes:
        live_ramp_textboxes['steps_step_period_s'].set_val(f"{defaults['steps_step_period_s']:.1f}")
    if 'slope_early_c_per_min' in live_ramp_textboxes:
        live_ramp_textboxes['slope_early_c_per_min'].set_val(f"{defaults['slope_early_c_per_min']:.2f}")
    if 'slope_mid_c_per_min' in live_ramp_textboxes:
        live_ramp_textboxes['slope_mid_c_per_min'].set_val(f"{defaults['slope_mid_c_per_min']:.2f}")
    if 'slope_late_c_per_min' in live_ramp_textboxes:
        live_ramp_textboxes['slope_late_c_per_min'].set_val(f"{defaults['slope_late_c_per_min']:.2f}")
    set_live_ramp_settings(
        mode=defaults['mode'],
        steps_until_temp_c=defaults['steps_until_temp_c'],
        steps_step_period_s=defaults['steps_step_period_s'],
        slope_early_c_per_min=defaults['slope_early_c_per_min'],
        slope_mid_c_per_min=defaults['slope_mid_c_per_min'],
        slope_late_c_per_min=defaults['slope_late_c_per_min'],
    )

def _set_live_action_status(message: str):
    global live_action_status_text
    formatted = str(message).replace(' | ', '\n')
    with live_action_status_lock:
        live_action_status_text = formatted

    # Direct GUI update is only safe from the main thread.
    if running_in_main_thread() and live_target_status_text is not None:
        live_target_status_text.set_text(formatted)
        try:
            fig.canvas.draw_idle()
        except Exception:
            pass


def _apply_live_action_status_to_gui():
    if live_target_status_text is None:
        return
    with live_action_status_lock:
        message = live_action_status_text
    if message and live_target_status_text.get_text() != message:
        live_target_status_text.set_text(message)


def get_manual_current_state():
    with manual_current_lock:
        return dict(manual_current_state)


def manual_current_is_enabled():
    return bool(get_manual_current_state().get('enabled', False))


def refresh_manual_current_button_styles():
    if not live_manual_buttons:
        return

    manual_enabled = manual_current_is_enabled()
    on_button = live_manual_buttons.get('set_manual_current')
    auto_button = live_manual_buttons.get('resume_auto_current')

    if on_button is not None:
        on_button.ax.set_facecolor('#fecaca' if manual_enabled else '#fee2e2')
        on_button.label.set_color('#991b1b')
        on_button.label.set_fontweight('bold' if manual_enabled else 'normal')

    if auto_button is not None:
        auto_button.ax.set_facecolor('#bbf7d0' if not manual_enabled else '#dcfce7')
        auto_button.label.set_color('#166534')
        auto_button.label.set_fontweight('bold' if not manual_enabled else 'normal')


def _manual_current_status_message(prefix=None):
    state = get_manual_current_state()
    mode = 'MANUAL ON' if state.get('enabled') else 'AUTO'
    requested = state.get('requested_current_a')
    applied = state.get('last_applied_current_a')
    parts = []
    if prefix:
        parts.append(prefix)
    parts.append(f'Keysight current mode: {mode}')
    if requested is not None:
        parts.append(f'requested I={float(requested):.3f} A')
    if applied is not None:
        parts.append(f'applied I={float(applied):.3f} A')
    parts.append(f'normal cap={normal_current_cap_a():.3f} A')
    return ' | '.join(parts)


def set_manual_current_enabled(enabled: bool, source: str = 'GUI', emit_message: bool = True):
    enabled = bool(enabled)
    with manual_current_lock:
        manual_current_state['enabled'] = enabled
        manual_current_state['last_changed_at'] = time.time()
        manual_current_state['last_hold_log_at'] = 0.0

    # Avoid a PID derivative kick or an immediate automated nudge when returning
    # from manual current control.
    reset_temperature_pid('Manual Keysight current control changed')
    keysight_state['last_step_at'] = time.time()

    if not enabled:
        keysight_state['hold_current_a'] = None

    if running_in_main_thread():
        refresh_manual_current_button_styles()

    if emit_message:
        message = _manual_current_status_message(
            f'Manual current control {"enabled" if enabled else "disabled"} from {source}'
        )
        print_banner(message)
        _set_live_action_status(message)


def _apply_manual_current_from_widgets(event=None):
    try:
        requested_current_a = float(live_manual_textboxes['manual_current_a'].text.strip().replace(',', '.'))
    except Exception as e:
        message = f'Could not parse manual Keysight current input: {e}'
        print(message)
        _set_live_action_status(message)
        return

    if requested_current_a < 0.0:
        message = 'Manual Keysight current must be >= 0 A.'
        print(message)
        _set_live_action_status(message)
        return

    with manual_current_lock:
        manual_current_state['requested_current_a'] = requested_current_a

    set_manual_current_enabled(True, 'GUI manual current control', emit_message=False)

    try:
        keysight_set_current(requested_current_a)
    except Exception as e:
        set_manual_current_enabled(False, 'manual current command failed', emit_message=False)
        message = f'Could not send manual Keysight current command: {e}'
        print(message)
        _set_live_action_status(message)
        if running_in_main_thread():
            refresh_manual_current_button_styles()
        return

    applied_current_a = keysight_state.get('set_current_a')
    keysight_state['hold_current_a'] = applied_current_a
    keysight_state['last_step_at'] = time.time()

    with manual_current_lock:
        manual_current_state['requested_current_a'] = requested_current_a
        manual_current_state['last_applied_current_a'] = applied_current_a
        manual_current_state['last_changed_at'] = time.time()

    if applied_current_a is None:
        message = 'Manual Keysight current command was sent, but the stored setpoint is unknown.'
    elif requested_current_a > float(applied_current_a) + 1e-9:
        message = (
            f'Manual Keysight current requested {requested_current_a:.3f} A, '
            f'clamped to {float(applied_current_a):.3f} A by the normal safety cap.'
        )
    else:
        message = f'Manual Keysight current applied: {float(applied_current_a):.3f} A.'

    print_banner(message)
    _set_live_action_status(message)
    if running_in_main_thread():
        refresh_manual_current_button_styles()


def _resume_automatic_current_control(event=None):
    set_manual_current_enabled(False, 'GUI Auto current button')


def confirm_shutter_open(source: str = 'GUI button'):
    with state_lock:
        process_state['shutter_open_confirmed'] = True
        process_state['shutter_close_confirmed'] = False

    message = f"Shutter OPEN confirmed from {source}."
    print_banner(message)
    _set_live_action_status(message)


def confirm_shutter_closed(source: str = 'GUI button'):
    with state_lock:
        process_state['shutter_close_confirmed'] = True
        process_state['shutter_open_confirmed'] = False

    message = f"Shutter CLOSED confirmed from {source}."
    print_banner(message)
    _set_live_action_status(message)

    # Keep a record of the manual close action without forcing the run to stop.
    # Use Abort / Finish for a controlled stop of the script and power supply.
    request_snapshot('shutter_closed')
    save_phase_summary('shutter_closed')



def request_gui_abort_or_finish(event=None):
    message = (
        'Abort requested from GUI. The script will run the controlled abort ramp-down '
        'and then switch the Keysight output OFF.'
    )
    print_banner(message)
    _set_live_action_status(message)

    with state_lock:
        process_state['phase'] = 'ABORT_RAMP_DOWN'
        process_state['transition_reason'] = message
        process_state['normal_completion'] = False
        process_state['leave_keysight_on_for_next_script'] = False

    try:
        request_snapshot('gui_abort_rampdown_start')
        save_phase_summary('gui_abort_rampdown_start')
    except Exception as e:
        print(f"Could not save GUI abort summary before rampdown: {e}")

    def _abort_rampdown_worker():
        try:
            rampdown_keysight_output('GUI Abort button')
        except Exception as e:
            print_banner(
                f"Abort rampdown failed internally: {e}\n"
                "Falling back to immediate Keysight zero/OFF."
            )
            emergency_keysight_shutdown(f'GUI abort rampdown failed: {e}')
            return
        finally:
            try:
                request_snapshot('gui_abort_rampdown_end')
                save_phase_summary('gui_abort_rampdown_end')
            except Exception:
                pass
            stop_event.set()

    threading.Thread(target=_abort_rampdown_worker, daemon=True).start()


def _gui_open_shutter(event=None):
    confirm_shutter_open('GUI button')


def _gui_close_shutter(event=None):
    confirm_shutter_closed('GUI button')



def _temperature_view_label(mode):
    return {
        'oven': 'Oven PID temperature',
        'pyrometer': 'Raw pyrometer temperature',
        'sample': 'Estimated sample temperature',
    }.get(mode, 'Oven PID temperature')


def _refresh_temperature_view_button_styles():
    colors = {
        'oven': ('#f7c6d8', '#b73364'),
        'pyrometer': ('#c8eef5', '#147a91'),
        'sample': ('#fde0bc', '#bd6418'),
    }
    active = temperature_view_state.get('mode', 'oven')
    for mode, button in temperature_view_buttons.items():
        inactive_color, active_color = colors[mode]
        selected = mode == active
        button.color = active_color if selected else inactive_color
        button.hovercolor = active_color if selected else '#ffffff'
        button.label.set_color('#ffffff' if selected else '#26384d')
        button.ax.set_facecolor(button.color)


def set_temperature_view(mode):
    if mode not in {'oven', 'pyrometer', 'sample'}:
        return
    temperature_view_state['mode'] = mode
    ax_temperature_oven.set_title(_temperature_view_label(mode), fontsize=10.6, fontweight='bold', color=AXIS_ACCENTS['temperature'], pad=8)
    _refresh_temperature_view_button_styles()
    try:
        fig.canvas.draw_idle()
    except Exception:
        pass
    print(f"Temperature graph view changed to: {_temperature_view_label(mode)}")


def setup_temperature_view_selector():
    """Create a compact three-button selector above the existing temperature graph."""

    if temperature_view_buttons:
        return
    bbox = ax_temperature_oven.get_position()
    gap = 0.004
    total_width = bbox.width
    button_width = (total_width - 2 * gap) / 3.0
    y = bbox.y1 + 0.040
    height = 0.021
    options = (
        ('oven', 'OVEN PID'),
        ('pyrometer', 'PYROMETER'),
        ('sample', 'SAMPLE EST.'),
    )
    for index, (mode, label) in enumerate(options):
        button_ax = fig.add_axes([bbox.x0 + index * (button_width + gap), y, button_width, height])
        button = Button(button_ax, label, color='#edf1f6', hovercolor='#ffffff')
        button.label.set_fontsize(7.7)
        button.label.set_fontweight('bold')
        button.on_clicked(lambda _event, selected=mode: set_temperature_view(selected))
        temperature_view_buttons[mode] = button
    set_temperature_view(temperature_view_state.get('mode', 'oven'))


def setup_live_target_controls():
    global live_target_status_text, live_dashboard_text, control_panel_ax

    panel_left = 0.765
    panel_bottom = 0.045
    panel_width = 0.220
    panel_height = 0.885

    control_panel_ax = fig.add_axes([panel_left, panel_bottom, panel_width, panel_height])
    control_panel_ax.set_facecolor('#f8fafc')
    for spine in control_panel_ax.spines.values():
        spine.set_color('#cbd5e1')
        spine.set_linewidth(1.1)
    control_panel_ax.set_xticks([])
    control_panel_ax.set_yticks([])

    # Presentation-only cards: equivalent controls use exactly the same visual
    # hierarchy and names in Phase 01 and Phase 03.
    add_panel_card(control_panel_ax, 0.025, 0.695, 0.950, 0.240, facecolor='#eef6ff', edgecolor='#bfdbfe')
    add_panel_card(control_panel_ax, 0.025, 0.380, 0.950, 0.305, facecolor='#f5f1ff', edgecolor='#ddd6fe')
    add_panel_card(control_panel_ax, 0.025, 0.270, 0.950, 0.100, facecolor='#fff7e8', edgecolor='#fde68a')
    add_panel_card(control_panel_ax, 0.025, 0.160, 0.950, 0.100, facecolor='#eefbf3', edgecolor='#bbf7d0')
    add_panel_card(control_panel_ax, 0.025, 0.010, 0.950, 0.140, facecolor='#ffffff', edgecolor='#d6dee8')

    def panel_text(x, y, text, fontsize=8.7, color='#334155', weight='normal', ha='left'):
        return control_panel_ax.text(
            x, y, text, transform=control_panel_ax.transAxes,
            fontsize=fontsize, color=color, fontweight=weight,
            va='top', ha=ha
        )

    def add_button(x_rel, y_rel, w_rel, h_rel, label, color, hovercolor, text_color, fontsize=7.8):
        ax_button = fig.add_axes([
            panel_left + panel_width * x_rel,
            panel_bottom + panel_height * y_rel,
            panel_width * w_rel,
            panel_height * h_rel,
        ])
        button = Button(ax_button, label, color=color, hovercolor=hovercolor)
        button.label.set_fontsize(fontsize)
        button.label.set_color(text_color)
        for spine in ax_button.spines.values():
            spine.set_color('#cbd5e1')
            spine.set_linewidth(0.9)
        return button

    def add_textbox(key, label, initial, y_rel, target='targets'):
        panel_text(0.05, y_rel + 0.035, label, fontsize=8.2)
        ax_box = fig.add_axes([
            panel_left + panel_width * 0.53,
            panel_bottom + panel_height * y_rel,
            panel_width * 0.36,
            panel_height * 0.027,
        ])
        ax_box.set_facecolor('white')
        for spine in ax_box.spines.values():
            spine.set_color('#cbd5e1')
            spine.set_linewidth(0.9)
        textbox = TextBox(ax_box, '', initial=initial)
        textbox.label.set_visible(False)
        textbox.text_disp.set_fontsize(8.7)
        if target == 'targets':
            textbox.on_submit(_apply_live_targets_from_widgets)
            live_target_textboxes[key] = textbox
        elif target == 'ramp':
            textbox.on_submit(_apply_live_ramp_settings_from_widgets)
            live_ramp_textboxes[key] = textbox
        elif target == 'manual':
            textbox.on_submit(_apply_manual_current_from_widgets)
            live_manual_textboxes[key] = textbox
        else:
            raise ValueError(f'Unknown textbox target: {target}')
        return textbox

    panel_text(0.05, 0.985, 'DP-DBBA Evaporation', fontsize=12.2, color='#0f172a', weight='bold')
    panel_text(0.05, 0.957, 'Live controls and run status', fontsize=8.1, color='#475569')

    # Editable heating targets
    panel_text(0.05, 0.920, 'Editable heating targets', fontsize=9.3, color='#334155', weight='bold')
    target_box_specs = [
        ('trigger_temp_c', 'Temp target (ºC)', f"{HEATING_TRIGGER_TEMP_C:.1f}", 0.850),
        ('rate_target_a_per_s', 'Target CK-1 rate (Å/s)', f"{CK1_RATE_TARGET_A_PER_S:.3f}", 0.800),
        ('pid_temp_band_c', 'PID band (ºC)', f"{PID_TEMP_BAND_C:.1f}", 0.750),
    ]
    for key, label, initial, y_rel in target_box_specs:
        add_textbox(key, label, initial, y_rel, target='targets')

    btn_apply = add_button(0.08, 0.705, 0.37, 0.035, 'Apply targets', '#dbeafe', '#bfdbfe', '#1e3a8a', fontsize=8.8)
    btn_apply.on_clicked(_apply_live_targets_from_widgets)
    live_target_buttons['apply'] = btn_apply

    btn_reset = add_button(0.52, 0.705, 0.37, 0.035, 'Reset targets', '#e2e8f0', '#cbd5e1', '#334155', fontsize=8.8)
    btn_reset.on_clicked(_reset_live_targets_to_defaults)
    live_target_buttons['reset'] = btn_reset

    # Ramp-up mode controls
    panel_text(0.05, 0.680, 'Ramp-up settings', fontsize=9.3, color='#334155', weight='bold')
    btn_steps = add_button(0.06, 0.625, 0.40, 0.034, 'Steps mode', '#eef2ff', '#c7d2fe', '#312e81', fontsize=7.1)
    btn_steps.on_clicked(_set_steps_ramp_mode)
    live_ramp_buttons[RAMP_MODE_STEPS] = btn_steps

    btn_slope = add_button(0.52, 0.625, 0.40, 0.034, 'Slope mode', '#eff6ff', '#bae6fd', '#075985', fontsize=7.1)
    btn_slope.on_clicked(_set_slope_ramp_mode)
    live_ramp_buttons[RAMP_MODE_SLOPE] = btn_slope

    add_textbox('steps_until_temp_c', 'Steps until T (ºC)', f"{STEPS_RAMP_UNTIL_TEMP_C:.1f}", 0.580, target='ramp')
    add_textbox('steps_step_period_s', 'Step period (s)', f"{STEPS_RAMP_STEP_PERIOD_S:.1f}", 0.545, target='ramp')
    add_textbox('slope_early_c_per_min', 'Slope early (ºC/min)', f"{TEMP_SLOPE_TARGET_EARLY_C_PER_MIN:.2f}", 0.510, target='ramp')
    add_textbox('slope_mid_c_per_min', 'Slope mid (ºC/min)', f"{TEMP_SLOPE_TARGET_MID_C_PER_MIN:.2f}", 0.475, target='ramp')
    add_textbox('slope_late_c_per_min', 'Slope late (ºC/min)', f"{TEMP_SLOPE_TARGET_LATE_C_PER_MIN:.2f}", 0.440, target='ramp')

    btn_ramp_apply = add_button(0.08, 0.392, 0.37, 0.032, 'Apply ramp', '#ede9fe', '#ddd6fe', '#5b21b6', fontsize=7.2)
    btn_ramp_apply.on_clicked(_apply_live_ramp_settings_from_widgets)
    live_ramp_buttons['apply_ramp'] = btn_ramp_apply

    btn_ramp_reset = add_button(0.52, 0.392, 0.37, 0.032, 'Reset ramp', '#f1f5f9', '#e2e8f0', '#334155', fontsize=7.2)
    btn_ramp_reset.on_clicked(_reset_live_ramp_settings_to_defaults)
    live_ramp_buttons['reset_ramp'] = btn_ramp_reset

    # Manual Keysight current control
    panel_text(0.05, 0.365, 'Manual current override', fontsize=9.1, color='#334155', weight='bold')
    add_textbox('manual_current_a', 'Manual I (A)', f"{KEYSIGHT_START_CURRENT_A:.3f}", 0.315, target='manual')

    btn_manual_set = add_button(0.06, 0.278, 0.40, 0.034, 'Set manual current', '#fee2e2', '#fecaca', '#991b1b', fontsize=6.8)
    btn_manual_set.on_clicked(_apply_manual_current_from_widgets)
    live_manual_buttons['set_manual_current'] = btn_manual_set

    btn_auto_current = add_button(0.52, 0.278, 0.40, 0.034, 'Resume automatic', '#dcfce7', '#bbf7d0', '#166534', fontsize=6.8)
    btn_auto_current.on_clicked(_resume_automatic_current_control)
    live_manual_buttons['resume_auto_current'] = btn_auto_current

    # Operator actions
    panel_text(0.05, 0.255, 'Operator controls', fontsize=9.3, color='#334155', weight='bold')
    btn_open = add_button(0.06, 0.213, 0.40, 0.036, 'Open shutter', '#dcfce7', '#bbf7d0', '#166534', fontsize=6.6)
    btn_open.on_clicked(_gui_open_shutter)
    live_target_buttons['open_shutter'] = btn_open

    btn_close = add_button(0.52, 0.213, 0.40, 0.036, 'Close shutter', '#ffedd5', '#fed7aa', '#9a3412', fontsize=6.6)
    btn_close.on_clicked(_gui_close_shutter)
    live_target_buttons['close_shutter'] = btn_close

    btn_abort = add_button(0.06, 0.172, 0.86, 0.038, 'Abort / safe stop', '#fee2e2', '#fecaca', '#991b1b', fontsize=7.2)
    btn_abort.on_clicked(request_gui_abort_or_finish)
    live_target_buttons['abort_finish'] = btn_abort

    # Run status and last action
    panel_text(0.05, 0.145, 'Process status', fontsize=9.8, color='#334155', weight='bold')
    live_dashboard_text = control_panel_ax.text(
        0.05, 0.128, '', transform=control_panel_ax.transAxes,
        fontsize=6.35, color='#0f172a', va='top', ha='left', linespacing=0.88,
        clip_on=True
    )

    panel_text(0.55, 0.145, 'Last action', fontsize=9.8, color='#334155', weight='bold')
    live_target_status_text = control_panel_ax.text(
        0.55, 0.128, '', transform=control_panel_ax.transAxes,
        fontsize=6.25, color='#334155', va='top', ha='left', linespacing=0.88,
        clip_on=True
    )

    set_live_heating_targets(
        HEATING_TRIGGER_TEMP_C,
        CK1_RATE_TARGET_A_PER_S,
        CK1_RATE_LOW_A_PER_S,
        CK1_RATE_HIGH_A_PER_S,
        PID_TEMP_BAND_C,
    )
    set_live_ramp_settings(
        mode=DEFAULT_RAMP_UP_MODE,
        steps_until_temp_c=STEPS_RAMP_UNTIL_TEMP_C,
        steps_step_period_s=STEPS_RAMP_STEP_PERIOD_S,
        slope_early_c_per_min=TEMP_SLOPE_TARGET_EARLY_C_PER_MIN,
        slope_mid_c_per_min=TEMP_SLOPE_TARGET_MID_C_PER_MIN,
        slope_late_c_per_min=TEMP_SLOPE_TARGET_LATE_C_PER_MIN,
    )
    refresh_manual_current_button_styles()

def show_live_plot_window():
    global LIVE_PLOT_ENABLED, LIVE_PLOT_FAILURE_REPORTED

    if not LIVE_PLOT_ENABLED:
        return

    try:
        manager = getattr(fig.canvas, 'manager', None)
        if manager is not None and hasattr(manager, 'set_window_title'):
            manager.set_window_title('Phase 03 · DP-DBBA Evaporation')
        if manager is not None and hasattr(manager, 'show'):
            manager.show()
        else:
            plt.show(block=False)
        fig.canvas.draw_idle()
        fig.canvas.start_event_loop(0.001)
    except Exception as e:
        LIVE_PLOT_ENABLED = False
        if not LIVE_PLOT_FAILURE_REPORTED:
            print(f'Could not open live plot window: {e}. Continuing without real-time plots.')
            LIVE_PLOT_FAILURE_REPORTED = True


def safe_live_plot_refresh(delay_s: float = 0.01):
    global LIVE_PLOT_ENABLED, LIVE_PLOT_FAILURE_REPORTED

    if not LIVE_PLOT_ENABLED:
        time.sleep(delay_s)
        return

    try:
        if not plt.fignum_exists(fig.number):
            LIVE_PLOT_ENABLED = False
            if not LIVE_PLOT_FAILURE_REPORTED:
                print('Live plot window closed or unavailable. Continuing without real-time plots.')
                LIVE_PLOT_FAILURE_REPORTED = True
            time.sleep(delay_s)
            return

        canvas = getattr(fig, 'canvas', None)
        manager = getattr(canvas, 'manager', None) if canvas is not None else None
        if canvas is None or manager is None:
            raise RuntimeError('Matplotlib canvas/manager is not available')

        window = getattr(manager, 'window', None)
        if hasattr(manager, 'window') and window is None:
            raise RuntimeError('Matplotlib window is not available')

        if hasattr(canvas, 'flush_events'):
            canvas.flush_events()
        elif hasattr(canvas, 'start_event_loop'):
            canvas.start_event_loop(0.001)
        time.sleep(delay_s)
    except Exception as e:
        LIVE_PLOT_ENABLED = False
        if not LIVE_PLOT_FAILURE_REPORTED:
            print(f'Live plot refresh failed: {e}. Continuing without real-time plots.')
            LIVE_PLOT_FAILURE_REPORTED = True
        time.sleep(delay_s)


def _thin_series(times, values, max_points=MAX_PLOT_POINTS_PER_SERIES):
    n = min(len(times), len(values))
    if n <= max_points:
        return times[:n], values[:n]

    step = max(1, n // max_points)
    thinned_times = times[:n:step]
    thinned_values = values[:n:step]

    if not thinned_times or thinned_times[-1] != times[n - 1]:
        thinned_times.append(times[n - 1])
        thinned_values.append(values[n - 1])

    return thinned_times, thinned_values


def copy_data_snapshot():
    with data_lock:
        return {
            device: {series_key: list(series_values) for series_key, series_values in series.items()}
            for device, series in data.items()
        }


def update_live_dashboard_text(snapshot=None):
    if live_dashboard_text is None:
        return

    targets = get_live_heating_targets()
    phase = current_phase()

    if snapshot is None:
        current_temp = latest_ck1_temperature()
        current_rate = latest_value('CK-1 evaporator QMB', 'rate_data')
        current_current = latest_value('Keysight power supply', 'current_data')
        current_voltage = latest_value('Keysight power supply', 'voltage_data')
    else:
        current_temp = snapshot['Arduino CK-1 crucible temperature']['temperature_data'][-1] if snapshot['Arduino CK-1 crucible temperature']['temperature_data'] else None
        current_rate = snapshot['CK-1 evaporator QMB']['rate_data'][-1] if snapshot['CK-1 evaporator QMB']['rate_data'] else None
        current_current = snapshot['Keysight power supply']['current_data'][-1] if snapshot['Keysight power supply']['current_data'] else None
        current_voltage = snapshot['Keysight power supply']['voltage_data'][-1] if snapshot['Keysight power supply']['voltage_data'] else None

    with state_lock:
        shutter_open = process_state.get('shutter_open_confirmed', False)
        shutter_closed = process_state.get('shutter_close_confirmed', False)

    if shutter_open:
        shutter_status = 'OPEN confirmed'
    elif shutter_closed:
        shutter_status = 'CLOSED confirmed'
    else:
        shutter_status = 'not confirmed'

    ramp_settings = get_live_ramp_settings()
    ramp_mode = ramp_mode_label(ramp_settings['mode'])
    manual_state = get_manual_current_state()
    manual_mode = 'ON' if manual_state.get('enabled') else 'AUTO'
    manual_applied = manual_state.get('last_applied_current_a')

    lines = [
        f'Phase: {phase}',
        f'Shutter: {shutter_status}',
        f'Current mode: Manual {manual_mode}',
        f'Manual I: {float(manual_applied):.3f} A' if manual_applied is not None else 'Manual I: --',
        f'Ramp: {ramp_mode}',
        f'Steps until: {ramp_settings["steps_until_temp_c"]:.1f} ºC',
        f'Step period: {ramp_settings["steps_step_period_s"]:.1f} s',
        f'Slopes E/M/L: {ramp_settings["slope_early_c_per_min"]:.1f}/'
        f'{ramp_settings["slope_mid_c_per_min"]:.1f}/'
        f'{ramp_settings["slope_late_c_per_min"]:.1f}',
        f'Target T: {targets["trigger_temp_c"]:.1f} ºC',
        f'Target rate: {targets["rate_target_a_per_s"]:.3f} Å/s',
        f'Band: {targets["rate_low_a_per_s"]:.3f}-{targets["rate_high_a_per_s"]:.3f}',
        f'DP target: {TARGET_CK1_THICKNESS_A:.2f} Å',
        f'CK-1 rel: {relative_ck1_thickness():.2f} Å' if relative_ck1_thickness() is not None else 'CK-1 rel: --',
        f'Sample rel: {relative_sample_thickness():.2f} Å' if relative_sample_thickness() is not None else 'Sample rel: --',
        f'CK-1 T: {current_temp:.1f} ºC' if current_temp is not None else 'CK-1 T: --',
        f'CK-1 rate: {current_rate:.3f} Å/s' if current_rate is not None else 'CK-1 rate: --',
        f'I: {current_current:.3f} A' if current_current is not None else 'I: --',
        f'V: {current_voltage:.3f} V' if current_voltage is not None else 'V: --',
    ]
    live_dashboard_text.set_text('\n'.join(lines))


def log_timestamp():
    ts = datetime.now()
    return ts, ts.strftime('%Y-%m-%d %H:%M:%S'), f"{ts.microsecond // 10000:02d}"


def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def latest_value(device_key, series_key):
    series = data[device_key][series_key]
    return series[-1] if series else None


def latest_ck1_temperature():
    return latest_value('Arduino CK-1 crucible temperature', 'temperature_data')


def latest_ck1_temperature_age_s():
    times = data['Arduino CK-1 crucible temperature']['temperature_times']
    if not times:
        return None
    try:
        return max(0.0, time.time() - times[-1].timestamp())
    except Exception:
        return None


def latest_ck1_temperature_timestamp():
    times = data['Arduino CK-1 crucible temperature']['temperature_times']
    return times[-1] if times else None


def latest_ck1_thickness():
    return latest_value('CK-1 evaporator QMB', 'thickness_data')


def latest_sample_thickness():
    return latest_value('Sample QMB', 'thickness_data')

def cumulative_qmb_thickness_for_display(qmb_name, plotted_last_value=None):
    """Return the QMB thickness accumulated since the script started.

    During EVAPORATION the plotted thickness is reset to the shutter-open
    window, so the graph label must use the raw QMB value for "last" and keep
    the reset-window value only for "rel".
    """
    raw_value = raw_qmb_last_values.get(qmb_name, {}).get('thickness')
    if raw_value is not None:
        return raw_value

    if plotted_last_value is None:
        return None

    try:
        return float(plotted_last_value) + float(qmb_thickness_offsets.get(qmb_name, 0.0))
    except Exception:
        return plotted_last_value


def average_ck1_rate(num_points=CK1_RATE_AVG_WINDOW_POINTS):
    rates = data['CK-1 evaporator QMB']['rate_data']
    if len(rates) < num_points:
        return None
    window = rates[-num_points:]
    return sum(window) / len(window)


def estimate_ck1_temp_slope_c_per_min(num_points=TEMP_SLOPE_WINDOW_POINTS):
    times = data['Arduino CK-1 crucible temperature']['temperature_times']
    temps = data['Arduino CK-1 crucible temperature']['temperature_data']

    if len(times) < num_points or len(temps) < num_points:
        return None

    recent_times = times[-num_points:]
    recent_temps = temps[-num_points:]

    t0 = recent_times[0]
    x = [(ts - t0).total_seconds() for ts in recent_times]
    y = recent_temps

    n = len(x)
    if n < 2:
        return None

    x_mean = sum(x) / n
    y_mean = sum(y) / n
    denom = sum((xi - x_mean) ** 2 for xi in x)
    if denom <= 0:
        return None

    slope_c_per_s = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y)) / denom
    return slope_c_per_s * 60.0


def target_ck1_temp_slope_c_per_min(current_a):
    settings = get_live_ramp_settings()
    if current_a < FAST_RAMP_CURRENT_THRESHOLD_A:
        return settings['slope_early_c_per_min']
    if current_a < MID_RAMP_CURRENT_THRESHOLD_A:
        return settings['slope_mid_c_per_min']
    return settings['slope_late_c_per_min']


def max_temp_control_step_a(current_a):
    if current_a < FAST_RAMP_CURRENT_THRESHOLD_A:
        return EARLY_RAMP_MAX_STEP_A
    if current_a < MID_RAMP_CURRENT_THRESHOLD_A:
        return MID_RAMP_MAX_STEP_A
    return LATE_RAMP_MAX_STEP_A


def apply_temperature_slope_control(current_setpoint):
    temp_slope = estimate_ck1_temp_slope_c_per_min()
    if temp_slope is None:
        changed = nudge_keysight_current(
            +KEYSIGHT_STEP_A,
            'Temperature-slope control waiting for enough temperature points; using fallback ramp step',
            max_current=KEYSIGHT_SOFT_WARNING_A
        )
        return changed, None, None, None

    target_slope = target_ck1_temp_slope_c_per_min(current_setpoint)
    slope_error = target_slope - temp_slope

    if abs(slope_error) <= TEMP_SLOPE_DEADBAND_C_PER_MIN:
        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.YELLOW}Temp-slope control: measured={temp_slope:.3f} ºC/min, "
            f"target={target_slope:.3f} ºC/min; inside deadband, holding current at "
            f"{current_setpoint:.3f} A{Style.RESET_ALL}"
        )
        return False, temp_slope, target_slope, 0.0

    delta_a = TEMP_SLOPE_KP_A_PER_C_PER_MIN * slope_error
    max_step = max_temp_control_step_a(current_setpoint)
    delta_a = clamp(delta_a, -max_step, +max_step)

    changed = nudge_keysight_current(
        delta_a,
        (
            f'Temp-slope control: measured={temp_slope:.3f} ºC/min, '
            f'target={target_slope:.3f} ºC/min, error={slope_error:.3f} ºC/min'
        ),
        max_current=KEYSIGHT_SOFT_WARNING_A
    )
    return changed, temp_slope, target_slope, delta_a


def clamp(value, low, high):
    return max(low, min(value, high))


def heating_ready_for_shutter(ck1_temp, ck1_rate_avg):
    """Return True when CK-1 is hot enough and the average rate has reached target.

    The upper rate limit is intentionally NOT used as a blocking condition here:
    if the average CK-1 rate goes above the old `rate_high` value, the script should
    still move to WAIT_SHUTTER_OPEN. Once the temperature target band is reached,
    the temperature PID hold keeps regulating the Keysight current.
    """
    target_temp = get_heating_trigger_temp_c()
    rate_target = get_ck1_rate_target_a_per_s()
    return (
        ck1_temp is not None and ck1_temp >= target_temp
        and ck1_rate_avg is not None
        and ck1_rate_avg >= rate_target
    )


def current_phase():
    with state_lock:
        return process_state['phase']


def set_phase(new_phase, reason=None):
    with state_lock:
        process_state['phase'] = new_phase
        process_state['phase_started_at'] = time.time()
        process_state['transition_reason'] = reason


def relative_sample_thickness():
    if not evaporation_measurement_active():
        return None
    baseline = process_state.get('baseline_sample_thickness')
    current = latest_sample_thickness()
    if baseline is None:
        baseline = 0.0
    if current is None:
        return None
    return current - baseline


def relative_ck1_thickness():
    if not evaporation_measurement_active():
        return None
    baseline = process_state.get('baseline_ck1_thickness')
    current = latest_ck1_thickness()
    if baseline is None:
        baseline = 0.0
    if current is None:
        return None
    return current - baseline


def evaporation_measurement_active():
    with state_lock:
        return bool(
            process_state.get('plots_reset_for_evaporation')
            or process_state.get('baseline_ck1_thickness') is not None
            or process_state.get('phase') in ('EVAPORATION', 'WAIT_SHUTTER_CLOSE', 'FINISHED')
        )


def reset_evaporation_measurement_window():
    """Reset the QMB evaporation window at shutter opening.

    This keeps the DP-DBBA evaporation graph and relative-thickness logic aligned:
    CK-1 and Sample thickness both restart from 0 Å when the operator confirms
    the shutter is open. Pressure, oven, Keysight current/voltage and CK-1
    temperature histories are kept.
    """
    with data_lock:
        for key in QMBs:
            raw_thickness = raw_qmb_last_values[key]['thickness']
            if raw_thickness is not None:
                qmb_thickness_offsets[key] = raw_thickness

        for qmb_name in QMBs:
            for series in data[qmb_name].values():
                series.clear()

    with state_lock:
        process_state['baseline_ck1_thickness'] = 0.0
        process_state['baseline_sample_thickness'] = 0.0
        process_state['plots_reset_for_evaporation'] = True

    global plot_refresh_counter
    plot_refresh_counter = 0


def request_snapshot(tag: str):
    with pending_snapshots_lock:
        pending_snapshots.append(tag)


def process_pending_snapshots():
    tags = []
    with pending_snapshots_lock:
        if pending_snapshots:
            tags = pending_snapshots[:]
            pending_snapshots.clear()

    for tag in tags:
        save_snapshot(tag)


def force_keysight_zero_output(reason: str = 'Force Keysight zero output'):
    try:
        key = 'Keysight power supply'
        ser = connections.get(key)
        if ser is not None and getattr(ser, 'is_open', True):
            with keysight_lock:
                for command in ('CURR 0.000', 'OUTP OFF', 'SYST:LOC'):
                    try:
                        ser.write((command + '\n').encode())
                        if hasattr(ser, 'flush'):
                            ser.flush()
                        time.sleep(0.08)
                    except Exception:
                        pass
        keysight_state['set_current_a'] = 0.0
        keysight_state['hold_current_a'] = 0.0
        keysight_state['automation_active'] = False
        keysight_state['reason_stopped'] = reason
    except Exception:
        pass


def normal_completion_leaves_keysight_on():
    with state_lock:
        return bool(
            process_state.get('normal_completion')
            and process_state.get('leave_keysight_on_for_next_script')
        )


def keysight_should_remain_on_after_stop():
    return normal_completion_leaves_keysight_on()


def mark_normal_completion_for_next_script():
    measured_current = latest_value('Keysight power supply', 'current_data')
    measured_voltage = latest_value('Keysight power supply', 'voltage_data')
    with state_lock:
        process_state['normal_completion'] = True
        process_state['leave_keysight_on_for_next_script'] = True
        process_state['automation_active_at_finish'] = keysight_state.get('automation_active')
        process_state['keysight_current_at_finish_a'] = keysight_state.get('set_current_a')
        process_state['keysight_measured_current_at_finish_a'] = measured_current
        process_state['keysight_voltage_at_finish_v'] = measured_voltage



def hold_keysight_for_next_script(reason: str):
    """Set the Keysight to base current and leave output ON for script 4.

    This is the normal end of DP-DBBA. The NPG Annealings script performs the
    later controlled ramp-down, so this script must not send CURR 0 or OUTP OFF.
    """
    measured_current_before = latest_value('Keysight power supply', 'current_data')
    if manual_current_is_enabled():
        set_manual_current_enabled(False, 'normal DP-DBBA handoff to script 4', emit_message=False)
    target_current = clamp(KEYSIGHT_BASE_WORK_CURRENT_A, 0.0, normal_current_cap_a())
    keysight_set_current(target_current)
    keysight_write('OUTP ON')
    keysight_state['hold_current_a'] = target_current
    keysight_state['automation_active'] = False
    keysight_state['reason_stopped'] = reason
    mark_normal_completion_for_next_script()
    print_banner(
        f"DP-DBBA NORMAL COMPLETION / HANDOFF TO SCRIPT 4\n"
        f"Keysight output is intentionally left ON.\n"
        f"Set current has been returned to base current = {target_current:.3f} A.\n"
        f"Last measured current before handoff = "
        f"{measured_current_before if measured_current_before is not None else '--'} A.\n"
        f"Do NOT run ramp-down here; start script 4 (NPG Annealings) for the next step."
    )
    return target_current


def leave_keysight_on_message(context: str):
    current_a = process_state.get('keysight_current_at_finish_a')
    measured_a = process_state.get('keysight_measured_current_at_finish_a')
    voltage_v = process_state.get('keysight_voltage_at_finish_v')
    print_banner(
        f"{context}: DP-DBBA normal handoff is active.\n"
        f"Keysight output is intentionally left ON and current is NOT set to 0 A.\n"
        f"Held set current = {current_a if current_a is not None else '--'} A | "
        f"last measured current = {measured_a if measured_a is not None else '--'} A | "
        f"last measured voltage = {voltage_v if voltage_v is not None else '--'} V.\n"
        f"Start script 4 to continue/ramp according to the NPG Annealings procedure."
    )


def emergency_keysight_shutdown(reason: str = 'Emergency shutdown'):
    with state_lock:
        process_state['normal_completion'] = False
        process_state['leave_keysight_on_for_next_script'] = False
    try:
        stop_event.set()
    except Exception:
        pass

    force_keysight_zero_output(reason)


def _apply_time_axis_format(ax):
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    ax.tick_params(axis='x', rotation=25, labelsize=8.5)
    ax.tick_params(axis='y', labelsize=8.5)
    ax.grid(True, alpha=0.25)
    ax.margins(x=0.02)


def _plot_series_for_snapshot(ax, times, values, title, ylabel, xlabel='', color=None):
    plot_kwargs = {'linewidth': 1.6}
    if color is not None:
        plot_kwargs['color'] = color
    ax.plot(times, values, **plot_kwargs)
    ax.set_title(title, fontsize=11, pad=10)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    _apply_time_axis_format(ax)


def _add_snapshot_reference_lines(ax_rate_ck1, ax_temperature_ck1):
    targets = get_live_heating_targets()
    ax_temperature_ck1.axhline(targets['trigger_temp_c'], linestyle='--', linewidth=1.1, color='black', alpha=0.8)
    ax_rate_ck1.axhline(targets['rate_target_a_per_s'], linestyle='--', linewidth=1.1, color='black', alpha=0.8)
    ax_rate_ck1.axhline(targets['rate_low_a_per_s'], linestyle=':', linewidth=1.0, color='gray', alpha=0.85)
    ax_rate_ck1.axhline(targets['rate_high_a_per_s'], linestyle=':', linewidth=1.0, color='gray', alpha=0.85)


def save_graphs_only_snapshot(tag: str, path: str, snapshot=None):
    """Save only the 3x3 measurement graphs, without GUI controls or full-screen capture."""
    source = copy_data_snapshot() if snapshot is None else snapshot

    snapshot_fig = Figure(figsize=(18, 14), constrained_layout=False)
    FigureCanvas(snapshot_fig)  # off-screen canvas: does not create a GUI window
    axes = snapshot_fig.subplots(3, 3)
    (fax_thickness_ck1, fax_rate_ck1, fax_pressure_xgs600), \
    (fax_thickness_sample, fax_rate_sample, fax_temperature_oven), \
    (fax_current_keysight, fax_voltage_keysight, fax_temperature_ck1) = axes

    snapshot_fig.patch.set_facecolor('white')
    title = f"DP-DBBA Evaporation snapshot: {tag} | CK-1 target = {TARGET_CK1_THICKNESS_A:.2f} Å"
    snapshot_fig.suptitle(title, fontsize=15, fontweight='bold')
    snapshot_fig.subplots_adjust(left=0.055, right=0.985, top=0.92, bottom=0.075, hspace=0.42, wspace=0.30)

    _plot_series_for_snapshot(
        fax_thickness_ck1,
        source['CK-1 evaporator QMB']['thickness_times'],
        source['CK-1 evaporator QMB']['thickness_data'],
        'CK-1 thickness',
        'Thickness (Å)',
        color='green',
    )
    _plot_series_for_snapshot(
        fax_rate_ck1,
        source['CK-1 evaporator QMB']['rate_times'],
        source['CK-1 evaporator QMB']['rate_data'],
        'CK-1 rate',
        'Rate (Å/s)',
        color='green',
    )
    _plot_series_for_snapshot(
        fax_pressure_xgs600,
        source['XGS600 HFIG pressure']['pressure_times'],
        source['XGS600 HFIG pressure']['pressure_data'],
        'Pressure',
        'Pressure (mbar)',
        color='blue',
    )
    _plot_series_for_snapshot(
        fax_thickness_sample,
        source['Sample QMB']['thickness_times'],
        source['Sample QMB']['thickness_data'],
        'Sample thickness',
        'Thickness (Å)',
        color='green',
    )
    _plot_series_for_snapshot(
        fax_rate_sample,
        source['Sample QMB']['rate_times'],
        source['Sample QMB']['rate_data'],
        'Sample rate',
        'Rate (Å/s)',
        color='green',
    )
    # Saved snapshots deliberately include every temperature signal, regardless
    # of which one the operator selected in the live GUI.  This preserves the
    # raw instrument data and the calibrated estimate for later comparison.
    fax_temperature_oven.plot(
        source['Oven PID temperature']['temperature_times'],
        source['Oven PID temperature']['temperature_data'],
        linewidth=1.6,
        color='magenta',
        label='Oven PID',
    )
    fax_temperature_oven.plot(
        source['IMPAC pyrometer']['temperature_times'],
        source['IMPAC pyrometer']['temperature_data'],
        linewidth=1.6,
        color='deepskyblue',
        label='Pyrometer raw',
    )
    fax_temperature_oven.plot(
        source['IMPAC pyrometer']['temperature_times'],
        source['IMPAC pyrometer']['sample_temperature_data'],
        linewidth=1.6,
        color='darkorange',
        label='Sample estimate',
    )
    fax_temperature_oven.set_title('Temperature comparison', fontsize=11, pad=10)
    fax_temperature_oven.set_xlabel('')
    fax_temperature_oven.set_ylabel('Temperature (ºC)')
    _apply_time_axis_format(fax_temperature_oven)
    fax_temperature_oven.legend(loc='best', fontsize=8)
    _plot_series_for_snapshot(
        fax_current_keysight,
        source['Keysight power supply']['current_times'],
        source['Keysight power supply']['current_data'],
        'Current',
        'Current (A)',
        xlabel='Time',
        color='goldenrod',
    )
    _plot_series_for_snapshot(
        fax_voltage_keysight,
        source['Keysight power supply']['voltage_times'],
        source['Keysight power supply']['voltage_data'],
        'Voltage',
        'Voltage (V)',
        xlabel='Time',
        color='goldenrod',
    )
    _plot_series_for_snapshot(
        fax_temperature_ck1,
        source['Arduino CK-1 crucible temperature']['temperature_times'],
        source['Arduino CK-1 crucible temperature']['temperature_data'],
        'CK-1 temp',
        'Temperature (ºC)',
        xlabel='Time',
        color='red',
    )

    _add_snapshot_reference_lines(fax_rate_ck1, fax_temperature_ck1)
    snapshot_fig.savefig(path, dpi=150)
    return path


def save_snapshot(tag: str):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(final_folder_path, f"{sample_name}_{tag}_{timestamp}.png")
    try:
        save_graphs_only_snapshot(tag, path)
        print(f"Saved graph-only snapshot: {path}")
        return path
    except Exception as e:
        print(f"Could not save graph-only snapshot '{tag}': {e}")
        return None


def save_phase_summary(tag: str):
    path = os.path.join(final_folder_path, f"{sample_name}_{tag}_summary.txt")
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"sample_name: {sample_name}\n")
            f.write(f"tag: {tag}\n")
            f.write(f"saved_at: {datetime.now().isoformat()}\n")
            f.write(f"phase: {current_phase()}\n")
            f.write(f"transition_reason: {process_state['transition_reason']}\n")
            f.write(f"ck1_temp_c: {latest_ck1_temperature()}\n")
            f.write(f"ck1_thickness_a: {latest_ck1_thickness()}\n")
            f.write(f"sample_thickness_a: {latest_sample_thickness()}\n")
            f.write(f"keysight_set_current_a: {keysight_state['set_current_a']}\n")
            f.write(f"keysight_hold_current_a: {keysight_state['hold_current_a']}\n")
            f.write(f"sample_relative_thickness_a: {relative_sample_thickness()}\n")
            f.write(f"ck1_relative_thickness_a: {relative_ck1_thickness()}\n")
            f.write(f"input_thickness_ratio: {input_thickness_ratio}\n")
            f.write(f"ideal_ck1_evaporation_thickness_a: {IDEAL_CK1_EVAPORATION_THICKNESS_A}\n")
            f.write(f"real_sample_thickness_a: {REAL_SAMPLE_THICKNESS_A}\n")
            f.write(f"target_ck1_thickness_a: {TARGET_CK1_THICKNESS_A}\n")
        print(f"Saved phase summary: {path}")
    except Exception as e:
        print(f"Could not save phase summary '{tag}': {e}")


def keysight_write(command: str, delay: float = 0.10):
    key = 'Keysight power supply'
    with keysight_lock:
        connections[key].write((command + '\n').encode())
        time.sleep(delay)


def keysight_query(command: str, delay: float = 0.10) -> str:
    key = 'Keysight power supply'
    with keysight_lock:
        connections[key].reset_input_buffer()
        connections[key].write((command + '\n').encode())
        time.sleep(delay)
        return connections[key].readline().decode(errors='ignore').strip()


def keysight_query_bool(command: str):
    """Return True/False for SCPI queries that answer 1/0, or None if parsing fails."""
    try:
        response = keysight_query(command)
        value = safe_float(response)
        if value is None:
            return None
        return bool(int(value))
    except Exception:
        return None


def keysight_protection_status():
    """Read Keysight output/protection status without changing the output state.

    These queries make the real reason visible if the supply latches OCP/OVP
    between two 1-second measurement points.
    """
    return {
        'output_on': keysight_query_bool('OUTP?'),
        'ocp_tripped': keysight_query_bool('CURR:PROT:TRIP?'),
        'ovp_tripped': keysight_query_bool('VOLT:PROT:TRIP?'),
    }


def normal_current_cap_a():
    """Current cap used by all normal automation commands."""
    return min(KEYSIGHT_SOFT_WARNING_A, KEYSIGHT_HARD_STOP_A)


def keysight_set_current(current_a: float):
    """Set Keysight current while enforcing the normal soft current cap.

    The automation never commands above KEYSIGHT_SOFT_WARNING_A. The higher
    KEYSIGHT_HARD_STOP_A is reserved only for the instrument OCP and abnormal
    measured-current safety checks.
    """
    requested_current = float(current_a)
    current_a = clamp(requested_current, 0.0, normal_current_cap_a())
    keysight_write(f'CURR {current_a:.3f}')
    keysight_state['set_current_a'] = current_a

    if requested_current > current_a + 1e-9:
        now = time.time()
        if now - keysight_state.get('last_soft_cap_warning_at', 0.0) >= 5.0:
            ts, formatted, dec = log_timestamp()
            print(
                f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                f"{Fore.YELLOW}Soft current cap enforced: requested "
                f"{requested_current:.3f} A, commanded {current_a:.3f} A "
                f"(soft cap {KEYSIGHT_SOFT_WARNING_A:.3f} A){Style.RESET_ALL}"
            )
            keysight_state['last_soft_cap_warning_at'] = now


def keysight_set_voltage_limit(voltage_v: float):
    """Set the normal compliance voltage limit.

    This is deliberately clamped to KEYSIGHT_VOLTAGE_LIMIT_V. It is not the
    emergency OVP threshold; OVP is configured with KEYSIGHT_HARD_STOP_V.
    """
    voltage_v = clamp(float(voltage_v), 0.0, KEYSIGHT_VOLTAGE_LIMIT_V)
    keysight_write(f'VOLT {voltage_v:.3f}')
    keysight_state['set_voltage_limit_v'] = voltage_v


def configure_keysight_for_automation():
    keysight_write('SYST:REM')
    keysight_write(f'VOLT:RANG {KEYSIGHT_RANGE}')
    keysight_write('*CLS')

    # The Keysight protection thresholds are hardware latches. They are kept
    # above the software hard stops to avoid nuisance trips from short transients.
    keysight_write(f'VOLT:PROT {KEYSIGHT_INSTRUMENT_OVP_V:.3f}')
    keysight_write('VOLT:PROT:STAT ON')
    keysight_write(f'CURR:PROT {KEYSIGHT_INSTRUMENT_OCP_A:.3f}')
    keysight_write('CURR:PROT:STAT ON')

    # Clear any stale protection latch from a previous run before enabling output.
    keysight_write('VOLT:PROT:CLE')
    keysight_write('CURR:PROT:CLE')

    # Normal voltage compliance limit and normal soft-capped current start.
    keysight_set_voltage_limit(KEYSIGHT_VOLTAGE_LIMIT_V)
    keysight_set_current(KEYSIGHT_START_CURRENT_A)

    keysight_write('OUTP ON')
    keysight_state['automation_active'] = True
    keysight_state['last_step_at'] = time.time()
    keysight_state['automation_started_at'] = time.time()
    keysight_state['reason_stopped'] = None


def stop_keysight_output(reason: str):
    force_keysight_zero_output(reason)
    ts, formatted, dec = log_timestamp()
    print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.YELLOW}Keysight output stopped: {reason}{Style.RESET_ALL}")


def nudge_keysight_current(delta_a: float, reason: str = '', max_current=None):
    current_setpoint = keysight_state.get('set_current_a')
    if current_setpoint is None:
        current_setpoint = latest_value('Keysight power supply', 'current_data')
    if current_setpoint is None:
        current_setpoint = KEYSIGHT_START_CURRENT_A

    upper_limit = normal_current_cap_a()
    if max_current is not None:
        upper_limit = min(upper_limit, float(max_current))

    next_current = clamp(current_setpoint + delta_a, 0.0, upper_limit)

    if abs(next_current - current_setpoint) < 1e-9:
        if delta_a > 0 and current_setpoint >= upper_limit - 1e-9:
            now = time.time()
            if now - keysight_state.get('last_soft_cap_warning_at', 0.0) >= 10.0:
                ts, formatted, dec = log_timestamp()
                print(
                    f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                    f"{Fore.YELLOW}Current held at cap {upper_limit:.3f} A; "
                    f"upward nudge blocked ({reason}){Style.RESET_ALL}"
                )
                keysight_state['last_soft_cap_warning_at'] = now
        return False

    keysight_set_current(next_current)
    keysight_state['hold_current_a'] = None

    ts, formatted, dec = log_timestamp()
    direction = 'up' if delta_a > 0 else 'down'
    print(
        f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
        f"{Fore.YELLOW}Keysight current nudge {direction}: "
        f"{current_setpoint:.3f} A -> {next_current:.3f} A ({reason}){Style.RESET_ALL}"
    )
    return True

def rampdown_keysight_output(reason: str) -> bool:
    """Safely ramp the evaporator current down before switching Keysight OFF.

    In this DP-DBBA script, this function is used only by the Abort button.
    Normal completion leaves the output ON at KEYSIGHT_BASE_WORK_CURRENT_A so
    script 4 can perform its own ramp-down sequence.
    """
    current = keysight_state.get('set_current_a')
    if current is None:
        current = latest_value('Keysight power supply', 'current_data') or 0.0
    current = max(0.0, float(current))

    keysight_state['automation_active'] = False
    keysight_state['hold_current_a'] = current
    keysight_state['reason_stopped'] = reason

    print_banner(
        f"PHASE: ABORT RAMP DOWN\n"
        f"Starting safe evaporator rampdown from {current:.3f} A.\n"
        f"Step: {RAMPDOWN_STEP_A:.3f} A every {RAMPDOWN_STEP_PERIOD_S:.0f} s.\n"
        f"The Keysight output will stay ON during rampdown and will switch OFF only at 0 A."
    )

    if current <= RAMPDOWN_ZERO_THRESHOLD_A:
        try:
            keysight_set_current(0.0)
        except Exception as e:
            print(f"Could not set Keysight current to 0 A before output OFF: {e}")
        stop_keysight_output(reason + ' - already at zero current')
        return True

    while current > RAMPDOWN_ZERO_THRESHOLD_A:
        if stop_event.is_set():
            stop_keysight_output('Abort rampdown interrupted by stop request')
            return False

        next_current = max(0.0, current - RAMPDOWN_STEP_A)
        if next_current <= RAMPDOWN_ZERO_THRESHOLD_A:
            next_current = 0.0

        keysight_set_current(next_current)
        keysight_state['hold_current_a'] = next_current

        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.YELLOW}ABORT RAMP DOWN: Keysight current {current:.3f} A -> "
            f"{next_current:.3f} A{Style.RESET_ALL}"
        )

        current = next_current
        if current <= RAMPDOWN_ZERO_THRESHOLD_A:
            break

        deadline = time.time() + RAMPDOWN_STEP_PERIOD_S
        while time.time() < deadline:
            if stop_event.is_set():
                stop_keysight_output('Abort rampdown interrupted by stop request')
                return False
            time.sleep(min(0.5, deadline - time.time()))

    try:
        keysight_set_current(0.0)
    except Exception as e:
        print(f"Could not send final CURR 0.000 command before output OFF: {e}")

    stop_keysight_output(reason + ' - abort rampdown complete at 0 A')
    return True


def print_banner(message: str):
    ts, formatted, dec = log_timestamp()
    print(f"\n{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.CYAN}{'='*80}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{message}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'='*80}{Style.RESET_ALL}\n")


def write_data_files(snapshot=None):
    source = data if snapshot is None else snapshot
    for title, data_dict in source.items():
        file_path = os.path.join(final_folder_path, f"{title.replace(' ', '_')}.txt")
        with open(file_path, 'w', encoding='utf-8') as file:
            for key, values in data_dict.items():
                file.write(f"{key}:\n")
                file.write("\n".join(map(str, values)) + "\n\n")


def phase_title_for_display(phase):
    mapping = {
        'HEATING_UP': 'HEATING UP',
        'WAIT_SHUTTER_OPEN': 'OPEN THE SHUTTER',
        'EVAPORATION': 'EVAPORATION',
        'WAIT_SHUTTER_CLOSE': 'CLOSE THE SHUTTER',
        'FINISHED': 'FINISHED',
        'ABORT_RAMP_DOWN': 'ABORT RAMP-DOWN',
        'SAFETY_STOP': 'SAFETY STOP',
    }
    return mapping.get(phase, str(phase).replace('_', ' '))

def update_live_plot(snapshot, force_autoscale=False):
    global plot_refresh_counter
    plot_refresh_counter += 1

    phase = current_phase()
    rel_sample = relative_sample_thickness()
    rel_ck1 = relative_ck1_thickness()
    autoscale_now = force_autoscale or (plot_refresh_counter % AUTOSCALE_EVERY_N_REFRESHES == 0)

    def _finite_float_values(values):
        finite = []
        for value in values:
            try:
                fv = float(value)
            except Exception:
                continue
            if math.isfinite(fv):
                finite.append(fv)
        return finite

    def _dynamic_y_limits(values, info_key):
        finite = _finite_float_values(values)
        if not finite:
            return None

        ymin = min(finite)
        ymax = max(finite)
        center = (ymin + ymax) / 2.0
        span = ymax - ymin

        min_spans = {
            'ck1_temp': 8.0,
            'oven_temp': 5.0,
            'ck1_rate': 0.030,
            'sample_rate': 0.030,
            'current': 0.030,
            'voltage': 0.080,
            'ck1_thickness': 0.20,
            'sample_thickness': 0.20,
        }
        if info_key == 'pressure':
            min_span = max(abs(center) * 0.15, 1e-12)
        else:
            min_span = min_spans.get(info_key, 0.1)

        if span < min_span:
            ymin = center - min_span / 2.0
            ymax = center + min_span / 2.0
            span = ymax - ymin

        margin = max(span * 0.12, min_span * 0.08)
        lower = ymin - margin
        upper = ymax + margin

        # Some variables cannot be negative in normal operation; this keeps early
        # plots readable without creating misleading negative space.
        if info_key in ('ck1_rate', 'sample_rate', 'current', 'voltage', 'ck1_thickness', 'sample_thickness'):
            lower = max(0.0, lower)
        if info_key == 'pressure' and lower <= 0:
            lower = min(finite) * 0.5 if min(finite) > 0 else 0.0

        if upper <= lower:
            upper = lower + min_span
        return lower, upper

    def _update_axis(ax, line, times, values, info_key, info_text):
        if not times or not values:
            axis_info_texts[info_key].set_text(info_text)
            return

        plot_times, plot_values = _thin_series(times, values)
        line.set_data(plot_times, plot_values)

        if autoscale_now or len(plot_times) <= 5:
            if len(plot_times) == 1:
                t0 = plot_times[0]
                ax.set_xlim(t0 - timedelta(seconds=30), t0 + timedelta(seconds=30))
            else:
                ax.set_xlim(min(plot_times), max(plot_times))

            limits = _dynamic_y_limits(plot_values, info_key)
            if limits is not None:
                ax.set_ylim(*limits)

        axis_info_texts[info_key].set_text(info_text)

    ck1_thickness_times = snapshot['CK-1 evaporator QMB']['thickness_times']
    ck1_thickness_values = snapshot['CK-1 evaporator QMB']['thickness_data']
    ck1_rate_times = snapshot['CK-1 evaporator QMB']['rate_times']
    ck1_rate_values = snapshot['CK-1 evaporator QMB']['rate_data']
    sample_thickness_times = snapshot['Sample QMB']['thickness_times']
    sample_thickness_values = snapshot['Sample QMB']['thickness_data']
    sample_rate_times = snapshot['Sample QMB']['rate_times']
    sample_rate_values = snapshot['Sample QMB']['rate_data']
    pressure_times = snapshot['XGS600 HFIG pressure']['pressure_times']
    pressure_values = snapshot['XGS600 HFIG pressure']['pressure_data']
    oven_temp_times = snapshot['Oven PID temperature']['temperature_times']
    oven_temp_values = snapshot['Oven PID temperature']['temperature_data']
    pyro_temp_times = snapshot['IMPAC pyrometer']['temperature_times']
    pyro_temp_values = snapshot['IMPAC pyrometer']['temperature_data']
    sample_estimated_values = snapshot['IMPAC pyrometer']['sample_temperature_data']
    current_times = snapshot['Keysight power supply']['current_times']
    current_values = snapshot['Keysight power supply']['current_data']
    voltage_times = snapshot['Keysight power supply']['voltage_times']
    voltage_values = snapshot['Keysight power supply']['voltage_data']
    ck1_temp_times = snapshot['Arduino CK-1 crucible temperature']['temperature_times']
    ck1_temp_values = snapshot['Arduino CK-1 crucible temperature']['temperature_data']

    ck1_th_plot_last = ck1_thickness_values[-1] if ck1_thickness_values else None
    ck1_th_last = cumulative_qmb_thickness_for_display('CK-1 evaporator QMB', ck1_th_plot_last)
    ck1_rate_last = ck1_rate_values[-1] if ck1_rate_values else None
    sample_th_plot_last = sample_thickness_values[-1] if sample_thickness_values else None
    sample_th_last = cumulative_qmb_thickness_for_display('Sample QMB', sample_th_plot_last)
    sample_rate_last = sample_rate_values[-1] if sample_rate_values else None
    pressure_last = pressure_values[-1] if pressure_values else None
    oven_last = oven_temp_values[-1] if oven_temp_values else None
    pyro_last_raw = pyro_temp_values[-1] if pyro_temp_values else None
    pyro_last = pyro_last_raw if pyro_last_raw is not None and math.isfinite(float(pyro_last_raw)) else None
    sample_last_raw = sample_estimated_values[-1] if sample_estimated_values else None
    sample_estimated_last = sample_last_raw if sample_last_raw is not None and math.isfinite(float(sample_last_raw)) else None
    current_last = current_values[-1] if current_values else None
    voltage_last = voltage_values[-1] if voltage_values else None
    ck1_temp_last = ck1_temp_values[-1] if ck1_temp_values else None

    _update_axis(
        ax_thickness_ck1, line_thickness_ck1, ck1_thickness_times, ck1_thickness_values, 'ck1_thickness',
        f'last {ck1_th_last:.2f} Å\nrel {rel_ck1:.2f} Å' if ck1_th_last is not None and rel_ck1 is not None else (f'last {ck1_th_last:.2f} Å' if ck1_th_last is not None else '--')
    )
    _update_axis(
        ax_rate_ck1, line_rate_ck1, ck1_rate_times, ck1_rate_values, 'ck1_rate',
        f'last {ck1_rate_last:.3f} Å/s' if ck1_rate_last is not None else '--'
    )
    _update_axis(
        ax_thickness_sample, line_thickness_sample, sample_thickness_times, sample_thickness_values, 'sample_thickness',
        f'last {sample_th_last:.2f} Å\nrel {rel_sample:.2f} Å' if sample_th_last is not None and rel_sample is not None else (f'last {sample_th_last:.2f} Å' if sample_th_last is not None else '--')
    )
    _update_axis(
        ax_rate_sample, line_rate_sample, sample_rate_times, sample_rate_values, 'sample_rate',
        f'last {sample_rate_last:.3f} Å/s' if sample_rate_last is not None else '--'
    )
    _update_axis(
        ax_pressure_xgs600, line_pressure_xgs600, pressure_times, pressure_values, 'pressure',
        f'{pressure_last:.2e} mbar' if pressure_last is not None else '--'
    )
    temperature_mode = temperature_view_state.get('mode', 'oven')
    if temperature_mode == 'pyrometer':
        display_times = pyro_temp_times
        display_values = pyro_temp_values
        display_info = (
            f'{pyro_last:.1f} ºC'
            if pyro_last is not None
            else str(pyrometer_state.get('status', 'waiting'))
        )
    elif temperature_mode == 'sample':
        display_times = pyro_temp_times
        display_values = sample_estimated_values
        if sample_estimated_last is not None:
            display_info = f'{sample_estimated_last:.1f} ºC'
            if pyro_last is not None and pyro_last < PYROMETER_PROFILE.minimum_valid_pyrometer_c:
                display_info += '\nWARNING: extrapolated'
        else:
            display_info = str(pyrometer_state.get('status', 'waiting'))
    else:
        display_times = oven_temp_times
        display_values = oven_temp_values
        display_info = f'{oven_last:.1f} ºC' if oven_last is not None else '--'

    ax_temperature_oven.set_title(_temperature_view_label(temperature_mode), fontsize=10.6, fontweight='bold', color=AXIS_ACCENTS['temperature'], pad=8)
    _update_axis(
        ax_temperature_oven, line_temperature_oven, display_times, display_values, 'oven_temp',
        display_info
    )
    _update_axis(
        ax_current_keysight, line_current_keysight, current_times, current_values, 'current',
        f'{current_last:.4f} A' if current_last is not None else '--'
    )
    _update_axis(
        ax_voltage_keysight, line_voltage_keysight, voltage_times, voltage_values, 'voltage',
        f'{voltage_last:.3f} V' if voltage_last is not None else '--'
    )
    _update_axis(
        ax_temperature_ck1, line_temperature_ck1, ck1_temp_times, ck1_temp_values, 'ck1_temp',
        f'{ck1_temp_last:.1f} ºC' if ck1_temp_last is not None else '--'
    )

    update_live_dashboard_text(snapshot)
    _apply_live_action_status_to_gui()
    update_phase_badge(phase_title_text, phase_title_for_display(phase))
    try:
        fig.canvas.draw_idle()
    except Exception:
        pass

# _____________________CONNECTIONS________________________________________________
last_request = {key: {'thickness': time.time(), 'rate': time.time()} for key in device_info}
connections = {}
for key in device_info:
    connections[key] = serial.Serial(
        port=device_info[key]['port'],
        baudrate=device_info[key]['baud_rate'],
        timeout=timeout
    )


def close_all_serial_connections(context: str = 'serial cleanup'):
    """Best-effort close of every serial handle owned by this phase.

    This is intentionally only a shutdown/cleanup helper. It does not change
    experiment parameters, setpoints, phase transitions, or measurement logic.
    """

    for device_name, ser in list(connections.items()):
        try:
            port_name = getattr(ser, 'port', device_info.get(device_name, {}).get('port', device_name))
            if getattr(ser, 'is_open', False):
                try:
                    if hasattr(ser, 'reset_input_buffer'):
                        ser.reset_input_buffer()
                except Exception:
                    pass
                try:
                    if hasattr(ser, 'reset_output_buffer'):
                        ser.reset_output_buffer()
                except Exception:
                    pass
                try:
                    ser.close()
                    print(f"{context}: closed serial port for {device_name} ({port_name}).")
                except Exception as exc:
                    print(f"{context}: could not close serial port for {device_name} ({port_name}): {exc}")
        except Exception as exc:
            print(f"{context}: serial cleanup warning for {device_name}: {exc}")

    _close_pyrometer_reader(context)

    # Short pause for Windows USB/COM drivers to release handles before the next phase opens them.
    time.sleep(0.5)


# _____________________READING THREADS____________________________________________
def monitor_qmb():
    while not stop_event.is_set():
        try:
            for key in QMBs:
                connections[key].write(QMB__commands['zero'])
                time.sleep(0.1)
                print(f"{key}: Zeroing command sent.")

            while not stop_event.is_set():
                current_time = time.time()
                for key in QMBs:
                    if current_time - last_request[key]['thickness'] >= 1.0:
                        connections[key].write(QMB__commands['thickness'])
                        time.sleep(0.1)
                        response_thickness = connections[key].read(connections[key].in_waiting or 64)
                        if response_thickness:
                            cropped_data = response_thickness[3:-3]
                            try:
                                thickness_value = float(cropped_data)
                                raw_qmb_last_values[key]['thickness'] = thickness_value
                                adjusted_thickness_value = thickness_value - qmb_thickness_offsets[key]
                                ts, formatted, dec = log_timestamp()
                                with data_lock:
                                    data[key]['thickness_times'].append(ts)
                                    data[key]['thickness_data'].append(adjusted_thickness_value)
                                print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.GREEN}{key} Thickness: {adjusted_thickness_value:.3f} Å (raw {thickness_value:.3f} Å){Style.RESET_ALL}")
                            except ValueError:
                                print(f"{key}: Failed to parse thickness.")
                        last_request[key]['thickness'] = current_time

                    if current_time - last_request[key]['rate'] >= 0.5:
                        connections[key].write(QMB__commands['rate'])
                        time.sleep(0.1)
                        response_rate = connections[key].read(connections[key].in_waiting or 64)
                        if response_rate:
                            cropped_data = response_rate[3:-3]
                            try:
                                rate_value = float(cropped_data)
                                raw_qmb_last_values[key]['rate'] = rate_value
                                ts, formatted, dec = log_timestamp()
                                with data_lock:
                                    data[key]['rate_times'].append(ts)
                                    data[key]['rate_data'].append(rate_value)
                                print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.GREEN}{key} Rate: {rate_value} Å/s{Style.RESET_ALL}")
                            except ValueError:
                                print(f"{key}: Failed to parse rate.")
                        last_request[key]['rate'] = current_time
                time.sleep(0.05)
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")
    for key in QMBs:
        connections[key].close()


def read_pressure():
    key = 'XGS600 HFIG pressure'
    while not stop_event.is_set():
        try:
            time.sleep(1)
            command = "#0002USYNTH\r"
            connections[key].write(command.encode())
            time.sleep(0.1)
            msg = connections[key].read(connections[key].in_waiting or 100).decode(errors='ignore').strip().lstrip('>')
            pressure_value = float(msg)
            ts, formatted, dec = log_timestamp()
            with data_lock:
                data[key]['pressure_times'].append(ts)
                data[key]['pressure_data'].append(pressure_value)
            print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.BLUE}Synthesis chamber pressure: {pressure_value:.2e} mbar{Style.RESET_ALL}")
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")
    connections[key].close()


def read_powersupply():
    key = 'Keysight power supply'
    while not stop_event.is_set():
        try:
            time.sleep(1)
            measured_voltage = safe_float(keysight_query('MEAS:VOLT?'))
            measured_current = safe_float(keysight_query('MEAS:CURR?'))

            if measured_voltage is None or measured_current is None:
                print('Keysight: failed to parse measurement.')
                continue

            ts, formatted, dec = log_timestamp()
            with data_lock:
                data[key]['current_times'].append(ts)
                data[key]['current_data'].append(measured_current)
                data[key]['voltage_times'].append(ts)
                data[key]['voltage_data'].append(measured_voltage)

            print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.YELLOW}CK-1 crucible coil current: {measured_current:.4f} A{Style.RESET_ALL}")
            print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.YELLOW}CK-1 crucible coil voltage: {measured_voltage:.4f} V{Style.RESET_ALL}")

            # Detect a hardware protection latch or an unexpected output-off state.
            # This is the key diagnostic for cases where current suddenly reads ~0 A
            # even though the previous 1-second readback was below the software limits.
            protection = keysight_protection_status()
            expected_current = keysight_state.get('set_current_a') or 0.0
            output_on = protection.get('output_on')
            ocp_tripped = protection.get('ocp_tripped')
            ovp_tripped = protection.get('ovp_tripped')

            unexpected_output_off = (
                output_on is False
                and expected_current > ZERO_CURRENT_THRESHOLD_A
                and current_phase() not in ('ABORT_RAMP_DOWN', 'FINISHED', 'SAFETY_STOP')
            )

            if ocp_tripped or ovp_tripped or unexpected_output_off:
                reasons = []
                if ocp_tripped:
                    reasons.append('Keysight OCP latch is TRIPPED')
                if ovp_tripped:
                    reasons.append('Keysight OVP latch is TRIPPED')
                if unexpected_output_off:
                    reasons.append(
                        f'Keysight output is OFF while script expected {expected_current:.3f} A'
                    )
                reason = '; '.join(reasons) or 'unexpected Keysight protection/output state'
                print_banner(
                    "KEYSIGHT PROTECTION / OUTPUT-OFF DETECTED\n"
                    f"{reason}\n"
                    f"Last readback: I={measured_current:.4f} A, V={measured_voltage:.4f} V.\n"
                    "The script will stop instead of silently continuing at 0 A."
                )
                stop_keysight_output(reason)
                stop_event.set()
                continue

            if measured_voltage >= KEYSIGHT_HARD_STOP_V:
                stop_keysight_output(
                    f'Hard voltage stop exceeded ({measured_voltage:.3f} V >= {KEYSIGHT_HARD_STOP_V:.3f} V)'
                )
                stop_event.set()

            if measured_current >= KEYSIGHT_HARD_STOP_A:
                stop_keysight_output(
                    f'Hard current stop exceeded ({measured_current:.3f} A >= {KEYSIGHT_HARD_STOP_A:.3f} A)'
                )
                stop_event.set()

            elif measured_current > KEYSIGHT_SOFT_WARNING_A + 0.003:
                # This should not happen from software commands because every command is
                # clamped. If readback is higher, re-apply the soft cap without treating
                # it as a hard-stop fault.
                now = time.time()
                if now - keysight_state.get('last_soft_cap_warning_at', 0.0) >= 5.0:
                    print(
                        f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                        f"{Fore.YELLOW}Measured current is above soft cap "
                        f"({measured_current:.4f} A > {KEYSIGHT_SOFT_WARNING_A:.3f} A); "
                        f"re-applying soft-capped setpoint.{Style.RESET_ALL}"
                    )
                    keysight_state['last_soft_cap_warning_at'] = now
                keysight_set_current(KEYSIGHT_SOFT_WARNING_A)

            if measured_voltage >= KEYSIGHT_VOLTAGE_LIMIT_V:
                current_setpoint = keysight_state.get('set_current_a')
                if current_setpoint is None:
                    current_setpoint = measured_current
                if current_setpoint and current_setpoint > 0.0:
                    now = time.time()
                    if now - keysight_state.get('last_voltage_limit_guard_at', 0.0) >= VOLTAGE_LIMIT_GUARD_COOLDOWN_S:
                        print(
                            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                            f"{Fore.YELLOW}Voltage compliance limit reached "
                            f"({measured_voltage:.3f} V >= {KEYSIGHT_VOLTAGE_LIMIT_V:.3f} V); "
                            f"reducing current by one soft step, without triggering hard stop.{Style.RESET_ALL}"
                        )
                        nudge_keysight_current(
                            -min(KEYSIGHT_STEP_A, float(current_setpoint)),
                            'Voltage compliance limit guard',
                            max_current=KEYSIGHT_SOFT_WARNING_A,
                        )
                        keysight_state['last_voltage_limit_guard_at'] = now

        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")

    try:
        if normal_completion_leaves_keysight_on():
            leave_keysight_on_message('Keysight monitor thread stop')
        else:
            keysight_write('OUTP OFF')
    except Exception:
        pass
    connections[key].close()


# _____________________OVEN PID RKC PROTOCOL HELPERS_____________________________
PID_EOT = b"\x04"
PID_ENQ = b"\x05"
PID_STX = b"\x02"
PID_ETX = b"\x03"
PID_ACK = b"\x06"
PID_NAK = b"\x15"
PID_ADDRESS = '00'


def pid_xor_bcc(identifier_plus_data_plus_etx: bytes) -> bytes:
    value = 0
    for b in identifier_plus_data_plus_etx:
        value ^= b
    return bytes([value])


def pid_parse_frame(raw: bytes):
    if raw == b'':
        return {'status': 'NO_RESPONSE', 'raw': raw}
    if raw == PID_ACK:
        return {'status': 'ACK', 'raw': raw}
    if raw == PID_NAK:
        return {'status': 'NAK', 'raw': raw}
    if raw == PID_EOT:
        return {'status': 'EOT', 'raw': raw}
    if len(raw) >= 5 and raw[0:1] == PID_STX:
        try:
            etx_index = raw.index(PID_ETX)
        except ValueError:
            return {'status': 'UNKNOWN_FRAME', 'raw': raw, 'decoded': raw.decode(errors='ignore')}

        core = raw[1:etx_index]
        if len(core) < 2:
            return {'status': 'SHORT_FRAME', 'raw': raw, 'decoded': raw.decode(errors='ignore')}

        ident = core[:2].decode(errors='ignore')
        data_text = core[2:].decode(errors='ignore')
        return {
            'status': 'DATA',
            'raw': raw,
            'decoded': raw.decode(errors='ignore'),
            'ident': ident,
            'data': data_text,
        }
    return {'status': 'UNKNOWN', 'raw': raw, 'decoded': raw.decode(errors='ignore')}


def pid_parse_numeric_ascii(data_text: str):
    stripped = data_text.strip()
    if stripped == '':
        return None
    try:
        return float(stripped)
    except ValueError:
        pass

    allowed = ''.join(ch for ch in stripped if ch.isdigit() or ch in '.-')
    if allowed in ('', '-', '.', '-.'):
        return None
    try:
        return float(allowed)
    except ValueError:
        return None


def pid_read_identifier_raw(identifier: str, wait_s: float = 0.15) -> bytes:
    key = 'Oven PID temperature'
    with pid_lock:
        connections[key].reset_input_buffer()
        connections[key].write(PID_EOT)
        time.sleep(0.05)
        connections[key].write(PID_ADDRESS.encode('ascii') + identifier.encode('ascii') + PID_ENQ)
        time.sleep(wait_s)
        return connections[key].read(connections[key].in_waiting or 64)


def pid_read_value(identifier: str):
    parsed = pid_parse_frame(pid_read_identifier_raw(identifier))
    value = None
    if parsed.get('status') == 'DATA':
        value = pid_parse_numeric_ascii(parsed.get('data', ''))
    return parsed, value


def pid_format_target_like_current_data(current_data: str, target_value: float) -> str:
    template = current_data.strip()
    if not template:
        raise ValueError('No current PID setpoint template is available.')

    negative = template.startswith('-')
    body = template[1:] if negative else template

    if '.' in body:
        left, right = body.split('.', 1)
        decimals = len(right)
        width_left = len(left)
        formatted = f"{round(target_value, decimals):0{width_left}.{decimals}f}"
        if target_value < 0 and not formatted.startswith('-'):
            formatted = '-' + formatted
        return formatted

    width = len(body)
    if not float(target_value).is_integer():
        raise ValueError(
            f"The PID returned S1 without decimals ('{template}'); target must be an integer."
        )
    integer_value = int(round(target_value))
    sign = '-' if integer_value < 0 else ''
    digits = str(abs(integer_value)).zfill(width)
    return sign + digits


def pid_write_s1(data_text: str) -> bytes:
    key = 'Oven PID temperature'
    body = b'S1' + data_text.encode('ascii') + PID_ETX
    frame = PID_EOT + PID_ADDRESS.encode('ascii') + PID_STX + body + pid_xor_bcc(body)
    with pid_lock:
        connections[key].reset_input_buffer()
        connections[key].write(frame)
        time.sleep(0.20)
        return connections[key].read(1)


def pid_ack_name(raw: bytes) -> str:
    if raw == PID_ACK:
        return 'ACK'
    if raw == PID_NAK:
        return 'NAK'
    if raw == PID_EOT:
        return 'EOT'
    if raw == b'':
        return 'NO_RESPONSE'
    return repr(raw)


def set_oven_pid_setpoint(target_c: float):
    print_banner(f'Setting Oven PID target temperature to {target_c:.1f} ºC')

    s1_before, sv_before = pid_read_value('S1')
    _m1_before, pv_before = pid_read_value('M1')

    if s1_before.get('status') != 'DATA' or sv_before is None:
        raise RuntimeError(
            f"Could not read PID setpoint S1 before writing. status={s1_before.get('status')} raw={s1_before.get('raw')}"
        )

    data_text = pid_format_target_like_current_data(s1_before['data'], target_c)
    write_reply = pid_write_s1(data_text)
    time.sleep(0.20)

    s1_after, sv_after = pid_read_value('S1')
    _m1_after, pv_after = pid_read_value('M1')

    report_lines = [
        f'written_at: {datetime.now().isoformat()}',
        f'target_temperature_c: {target_c:.1f}',
        f's1_before_status: {s1_before.get("status")}',
        f's1_before_data: {s1_before.get("data")}',
        f's1_before_value: {sv_before}',
        f'm1_before_value: {pv_before}',
        f'write_reply: {pid_ack_name(write_reply)}',
        f's1_after_status: {s1_after.get("status")}',
        f's1_after_data: {s1_after.get("data")}',
        f's1_after_value: {sv_after}',
        f'm1_after_value: {pv_after}',
    ]

    report_path = os.path.join(final_folder_path, f'{sample_name}_oven_pid_setpoint.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines) + '\n')

    if sv_after is None or abs(sv_after - target_c) > 0.51:
        raise RuntimeError(
            f"PID did not confirm the requested setpoint. Reply={pid_ack_name(write_reply)}, readback={sv_after}"
        )

    oven_pid_state['setpoint_c'] = sv_after
    oven_pid_state['last_confirmed_pv_c'] = pv_after

    print(
        f"Oven PID target confirmed: PV={pv_after if pv_after is not None else '--'} ºC | "
        f"SV={sv_after:.1f} ºC | reply={pid_ack_name(write_reply)}"
    )



def _close_pyrometer_reader(context='pyrometer cleanup'):
    global pyrometer_reader
    with pyrometer_reader_lock:
        reader = pyrometer_reader
        pyrometer_reader = None
    if reader is None:
        return
    try:
        reader.close()
        print(f"{context}: closed IMPAC pyrometer port ({PYROMETER_SERIAL_CONFIG.port}).")
    except Exception as exc:
        print(f"{context}: pyrometer cleanup warning: {exc}")


def _append_pyrometer_reading(timestamp, raw_c, sample_c, status):
    with data_lock:
        data['IMPAC pyrometer']['temperature_times'].append(timestamp)
        data['IMPAC pyrometer']['temperature_data'].append(raw_c)
        data['IMPAC pyrometer']['sample_temperature_data'].append(sample_c)
        data['IMPAC pyrometer']['status_data'].append(status)

    # Append immediately so a controlled stop or unexpected GUI closure does
    # not lose the final pyrometer samples waiting for the periodic data save.
    try:
        with open(pyrometer_csv_path, 'a', newline='', encoding='utf-8') as pyrometer_csv_file:
            csv.writer(pyrometer_csv_file).writerow(
                [
                    timestamp.isoformat(timespec='milliseconds'),
                    raw_c,
                    sample_c,
                    status,
                    PYROMETER_PROFILE.profile_name,
                    PYROMETER_PROFILE.emissivity_percent,
                ]
            )
    except Exception as exc:
        pyrometer_state['last_error'] = f'CSV write warning: {exc}'


def read_pyrometer():
    """Monitor the IPE 140 without affecting any chamber-control decision."""

    global pyrometer_reader
    if not PYROMETER_PROFILE.enabled:
        pyrometer_state['status'] = 'disabled by launcher profile'
        print('IMPAC pyrometer monitoring is disabled for this launcher run.')
        return

    emissivity_setup_attempted = False
    while not stop_event.is_set():
        reader = ImpacIPE140(PYROMETER_SERIAL_CONFIG)
        with pyrometer_reader_lock:
            pyrometer_reader = reader
        try:
            reader.open()
            pyrometer_state['status'] = 'connected'
            pyrometer_state['last_error'] = ''

            # Emissivity setup is useful but must never prevent temperature
            # monitoring. Verification uses the same 11-digit `pa` reply that
            # succeeded in the standalone hardware diagnostic.
            confirmed = None
            emissivity_changed = False
            try:
                if PYROMETER_PROFILE.write_emissivity_at_start and not emissivity_setup_attempted:
                    emissivity_setup_attempted = True
                    confirmed, emissivity_changed = reader.ensure_emissivity_percent(
                        PYROMETER_PROFILE.emissivity_percent
                    )
                else:
                    confirmed = reader.read_emissivity_percent()
                pyrometer_state['confirmed_emissivity_percent'] = confirmed
                emissivity_action = 'updated and verified' if emissivity_changed else 'verified'
                print(
                    f"IMPAC pyrometer connected on {PYROMETER_SERIAL_CONFIG.port} at "
                    f"{PYROMETER_SERIAL_CONFIG.baudrate} baud; emissivity {emissivity_action} "
                    f"at {confirmed:.0f}%."
                )
            except Exception as setup_exc:
                pyrometer_state['confirmed_emissivity_percent'] = None
                pyrometer_state['last_error'] = f'Emissivity setup warning: {setup_exc}'
                print(
                    'Pyrometer emissivity could not be verified or changed, but raw temperature '
                    f'monitoring will continue. Warning: {setup_exc}'
                )

            while not stop_event.is_set():
                time.sleep(1.0)
                timestamp, formatted, dec = log_timestamp()
                try:
                    raw_c = reader.read_temperature_c()
                    sample_c = PYROMETER_PROFILE.estimated_sample_c(raw_c)
                    status = PYROMETER_PROFILE.calibration_status(raw_c)
                    _append_pyrometer_reading(timestamp, raw_c, sample_c, status)
                    warning_text = '' if status == 'OK' else ' | WARNING: extrapolated below calibrated range'
                    print(
                        f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                        f"{Fore.CYAN}Pyrometer: {raw_c:.1f} ºC | Estimated sample: "
                        f"{sample_c:.1f} ºC{warning_text}{Style.RESET_ALL}"
                    )
                    pyrometer_state['status'] = 'connected' if status == 'OK' else 'connected; extrapolating below calibrated range'
                except Exception as exc:
                    pyrometer_state['status'] = 'temporarily unavailable'
                    pyrometer_state['last_error'] = str(exc)
                    print(f"Pyrometer read warning (phase continues): {exc}")
                    raise
        except Exception as exc:
            pyrometer_state['status'] = 'unavailable; retrying'
            pyrometer_state['last_error'] = str(exc)
            timestamp = datetime.now()
            _append_pyrometer_reading(timestamp, float('nan'), float('nan'), f'ERROR: {exc}')
            print(
                f"IMPAC pyrometer unavailable on {PYROMETER_SERIAL_CONFIG.port}; "
                f"the phase continues and monitoring will retry in 5 s. Error: {exc}"
            )
        finally:
            try:
                reader.close()
            except Exception:
                pass
            with pyrometer_reader_lock:
                if pyrometer_reader is reader:
                    pyrometer_reader = None

        deadline = time.time() + 5.0
        while time.time() < deadline and not stop_event.is_set():
            time.sleep(0.2)


def read_PID():
    key = 'Oven PID temperature'
    while not stop_event.is_set():
        try:
            time.sleep(1)
            parsed, temperature_value = pid_read_value('M1')
            if temperature_value is None:
                raise ValueError(
                    f"Could not parse Oven PID PV from frame: status={parsed.get('status')} raw={parsed.get('raw')}"
                )
            ts, formatted, dec = log_timestamp()
            with data_lock:
                data[key]['temperature_times'].append(ts)
                data[key]['temperature_data'].append(temperature_value)
            print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.MAGENTA}Oven PID temperature: {temperature_value:.0f} ºC{Style.RESET_ALL}")
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")
    connections[key].close()

def read_arduino():
    key = 'Arduino CK-1 crucible temperature'
    while not stop_event.is_set():
        try:
            time.sleep(1)
            while not stop_event.is_set():
                if connections[key].in_waiting > 0:
                    arduino_message = connections[key].readline().decode('utf-8').strip()
                    ck1_temperature_value = float(arduino_message)
                    ts, formatted, dec = log_timestamp()
                    with data_lock:
                        data[key]['temperature_times'].append(ts)
                        data[key]['temperature_data'].append(ck1_temperature_value)
                    print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.RED}CK-1 crucible temperature: {ck1_temperature_value:.2f} ºC{Style.RESET_ALL}")
                else:
                    time.sleep(0.1)
        except serial.SerialException as e:
            print(f"Serial error in thread for {key}: {e}")
        except Exception as e:
            print(f"Error in thread for {key}: {e}")
    connections[key].close()


# _____________________TEMPERATURE PID CONTROL___________________________________
def reset_temperature_pid(reason: str = ''):
    temperature_pid_state['integral_error_c_s'] = 0.0
    temperature_pid_state['last_error_c'] = None
    temperature_pid_state['last_time'] = None
    if reason:
        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.CYAN}Temperature PID reset: {reason}{Style.RESET_ALL}"
        )


def apply_temperature_pid_control(current_temp, current_setpoint=None):
    """Regulate Keysight current to hold CK-1 temperature around the live target."""
    if current_temp is None:
        return False

    target_temp = get_heating_trigger_temp_c()
    band_c = get_pid_temp_band_c()
    now = time.time()

    if current_setpoint is None:
        current_setpoint = keysight_state.get('set_current_a')
    if current_setpoint is None:
        current_setpoint = latest_value('Keysight power supply', 'current_data') or 0.0

    error_c = target_temp - current_temp

    last_time = temperature_pid_state['last_time']
    last_error = temperature_pid_state['last_error_c']
    if last_time is None or last_error is None:
        temperature_pid_state['last_time'] = now
        temperature_pid_state['last_error_c'] = error_c
        temperature_pid_state['integral_error_c_s'] = 0.0
        return False

    dt = max(0.1, now - last_time)

    # Inside the safe band, do not chase noise. Keep the present current and
    # slowly discharge the integral term to avoid wind-up.
    if abs(error_c) <= band_c:
        temperature_pid_state['integral_error_c_s'] *= 0.90
        temperature_pid_state['last_time'] = now
        temperature_pid_state['last_error_c'] = error_c

        if now - temperature_pid_state.get('last_log_at', 0.0) >= 15.0:
            ts, formatted, dec = log_timestamp()
            print(
                f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                f"{Fore.YELLOW}Temperature PID hold: T={current_temp:.2f} ºC, "
                f"target={target_temp:.2f} ºC, band=±{band_c:.2f} ºC; "
                f"holding current at {current_setpoint:.3f} A{Style.RESET_ALL}"
            )
            temperature_pid_state['last_log_at'] = now
        return False

    integral = temperature_pid_state['integral_error_c_s'] + error_c * dt
    integral = clamp(integral, -PID_INTEGRAL_LIMIT_C_S, PID_INTEGRAL_LIMIT_C_S)
    derivative = (error_c - last_error) / dt

    raw_delta_a = (
        PID_KP_A_PER_C * error_c
        + PID_KI_A_PER_C_S * integral
        + PID_KD_A_PER_C_PER_S * derivative
    )
    delta_a = clamp(raw_delta_a, -PID_MAX_STEP_A, PID_MAX_STEP_A)

    # Avoid asking for impossible tiny changes after clamping/rounding.
    if abs(delta_a) < 1e-6:
        temperature_pid_state['integral_error_c_s'] = integral
        temperature_pid_state['last_time'] = now
        temperature_pid_state['last_error_c'] = error_c
        return False

    changed = nudge_keysight_current(
        delta_a,
        (
            f'Temperature PID: T={current_temp:.2f} ºC, target={target_temp:.2f} ºC, '
            f'error={error_c:+.2f} ºC, P/I/D delta={raw_delta_a:+.5f} A'
        ),
        max_current=KEYSIGHT_SOFT_WARNING_A,
    )

    if changed:
        keysight_state['last_step_at'] = now

    temperature_pid_state['integral_error_c_s'] = integral
    temperature_pid_state['last_time'] = now
    temperature_pid_state['last_error_c'] = error_c
    return changed


def should_use_temperature_pid(current_temp):
    """Use PID when the CK-1 has reached the live target band."""
    if current_temp is None:
        return False
    return current_temp >= (get_heating_trigger_temp_c() - get_pid_temp_band_c())


def _temperature_watchdog_active_now():
    return (
        TEMP_WATCHDOG_ENABLED
        and keysight_state.get('automation_active', False)
        and current_phase() in TEMP_WATCHDOG_ACTIVE_PHASES
        and not temperature_watchdog_state.get('hard_stop_triggered', False)
    )


def _temperature_watchdog_hard_stop(reason: str):
    temperature_watchdog_state['hard_stop_triggered'] = True
    reset_temperature_pid('Temperature watchdog hard stop')
    set_phase('SAFETY_STOP', reason)
    request_snapshot('temperature_watchdog_hard_stop')
    save_phase_summary('temperature_watchdog_hard_stop')
    stop_keysight_output(reason)
    stop_event.set()
    print_banner(
        "TEMPERATURE WATCHDOG HARD STOP\n"
        f"{reason}\n"
        "Keysight current was forced to 0 A and output was switched OFF."
    )


def _temperature_watchdog_soft_action(current_temp: float, soft_limit: float, target_temp: float):
    now = time.time()
    if now - temperature_watchdog_state.get('last_soft_action_at', 0.0) < TEMP_WATCHDOG_SOFT_COOLDOWN_S:
        return

    reset_temperature_pid('Temperature watchdog soft over-temperature')
    current_setpoint = keysight_state.get('set_current_a')
    if current_setpoint is None:
        current_setpoint = latest_value('Keysight power supply', 'current_data') or 0.0

    step = min(TEMP_WATCHDOG_SOFT_STEP_A, max(0.0, float(current_setpoint)))
    if step > 0:
        changed = nudge_keysight_current(
            -step,
            (
                f'Temperature watchdog soft limit: T={current_temp:.2f} ºC, '
                f'target={target_temp:.2f} ºC, soft limit={soft_limit:.2f} ºC'
            ),
            max_current=KEYSIGHT_SOFT_WARNING_A,
        )
        if changed:
            keysight_state['last_step_at'] = now

    temperature_watchdog_state['last_soft_action_at'] = now
    if now - temperature_watchdog_state.get('last_log_at', 0.0) >= 5.0:
        print_banner(
            "TEMPERATURE WATCHDOG SOFT ACTION\n"
            f"CK-1 temperature {current_temp:.2f} ºC is above soft limit {soft_limit:.2f} ºC.\n"
            f"Forced current reduction by up to {TEMP_WATCHDOG_SOFT_STEP_A:.3f} A.\n"
            f"Hard stop remains at target + {TEMP_WATCHDOG_HARD_MARGIN_C:.1f} ºC."
        )
        temperature_watchdog_state['last_log_at'] = now


def temperature_safety_watchdog():
    """Independent CK-1 temperature safety layer.

    This watchdog does not depend on the PID output. It watches the live CK-1
    Arduino temperature and the freshness/plausibility of that sensor while the
    Keysight automation is active.
    """
    while not stop_event.is_set():
        try:
            time.sleep(TEMP_WATCHDOG_PERIOD_S)
            if not _temperature_watchdog_active_now():
                continue

            now = time.time()
            target_temp = get_heating_trigger_temp_c()
            soft_limit = target_temp + TEMP_WATCHDOG_SOFT_MARGIN_C
            hard_limit = target_temp + TEMP_WATCHDOG_HARD_MARGIN_C
            current_temp = latest_ck1_temperature()
            temp_timestamp = latest_ck1_temperature_timestamp()
            temp_age_s = latest_ck1_temperature_age_s()
            automation_started_at = keysight_state.get('automation_started_at') or now

            if current_temp is None:
                if now - automation_started_at > TEMP_WATCHDOG_SENSOR_INITIAL_GRACE_S:
                    _temperature_watchdog_hard_stop(
                        'CK-1 temperature watchdog: no Arduino temperature reading '
                        f'after {TEMP_WATCHDOG_SENSOR_INITIAL_GRACE_S:.0f} s of active Keysight automation.'
                    )
                continue

            if temp_age_s is None or temp_age_s > TEMP_WATCHDOG_SENSOR_STALE_TIMEOUT_S:
                _temperature_watchdog_hard_stop(
                    'CK-1 temperature watchdog: Arduino temperature reading is stale '
                    f'(age={temp_age_s if temp_age_s is not None else "unknown"} s; '
                    f'limit={TEMP_WATCHDOG_SENSOR_STALE_TIMEOUT_S:.0f} s).'
                )
                continue

            if not math.isfinite(float(current_temp)) or not (TEMP_WATCHDOG_VALID_MIN_C <= float(current_temp) <= TEMP_WATCHDOG_VALID_MAX_C):
                _temperature_watchdog_hard_stop(
                    'CK-1 temperature watchdog: impossible temperature reading '
                    f'{current_temp!r} ºC outside valid range '
                    f'[{TEMP_WATCHDOG_VALID_MIN_C:.0f}, {TEMP_WATCHDOG_VALID_MAX_C:.0f}] ºC.'
                )
                continue

            last_temp = temperature_watchdog_state.get('last_temp_c')
            last_timestamp = temperature_watchdog_state.get('last_temp_timestamp')
            if (
                last_temp is not None
                and temp_timestamp is not None
                and last_timestamp is not None
                and temp_timestamp != last_timestamp
                and abs(float(current_temp) - float(last_temp)) > TEMP_WATCHDOG_MAX_JUMP_C
            ):
                _temperature_watchdog_hard_stop(
                    'CK-1 temperature watchdog: sudden unphysical temperature jump '
                    f'{last_temp:.2f} ºC -> {float(current_temp):.2f} ºC '
                    f'(limit={TEMP_WATCHDOG_MAX_JUMP_C:.1f} ºC between readings).'
                )
                continue

            temperature_watchdog_state['last_temp_c'] = float(current_temp)
            temperature_watchdog_state['last_temp_timestamp'] = temp_timestamp

            if float(current_temp) >= hard_limit:
                _temperature_watchdog_hard_stop(
                    'CK-1 temperature watchdog: hard limit exceeded '
                    f'({float(current_temp):.2f} ºC >= {hard_limit:.2f} ºC; '
                    f'target={target_temp:.2f} ºC).'
                )
                continue

            if float(current_temp) >= soft_limit:
                _temperature_watchdog_soft_action(float(current_temp), soft_limit, target_temp)

        except Exception as e:
            _temperature_watchdog_hard_stop(f'CK-1 temperature watchdog failed internally: {e}')


# _____________________AUTOMATION THREADS_________________________________________
def automate_keysight_heating():
    if not keysight_state['automation_enabled']:
        return

    try:
        configure_keysight_for_automation()
        initial_ramp = get_live_ramp_settings()
        print_banner(
            f"Keysight automation started. Current starts at {KEYSIGHT_START_CURRENT_A:.3f} A "
            f"and ramps up using the selected ramp-up mode until the CK-1 approaches the "
            f"editable temperature target.\n"
            f"Temperature PID then keeps CK-1 around target ±{get_pid_temp_band_c():.1f} ºC.\n"
            f"Independent temperature watchdog: soft action at target + {TEMP_WATCHDOG_SOFT_MARGIN_C:.1f} ºC; "
            f"hard stop at target + {TEMP_WATCHDOG_HARD_MARGIN_C:.1f} ºC; "
            f"sensor stale timeout {TEMP_WATCHDOG_SENSOR_STALE_TIMEOUT_S:.0f} s.\n"
            f"Normal current cap: {KEYSIGHT_SOFT_WARNING_A:.3f} A. "
            f"Software hard current stop: {KEYSIGHT_HARD_STOP_A:.3f} A. "
            f"Keysight OCP latch: {KEYSIGHT_INSTRUMENT_OCP_A:.3f} A.\n"
            f"Voltage compliance limit: {KEYSIGHT_VOLTAGE_LIMIT_V:.3f} V. "
            f"Software hard voltage stop: {KEYSIGHT_HARD_STOP_V:.3f} V. "
            f"Keysight OVP latch: {KEYSIGHT_INSTRUMENT_OVP_V:.3f} V.\n"
            f"Initial ramp mode: {ramp_mode_label(initial_ramp['mode'])}; "
            f"fixed steps of {KEYSIGHT_STEP_A:.3f} A every "
            f"{initial_ramp['steps_step_period_s']:.1f} s until "
            f"{initial_ramp['steps_until_temp_c']:.1f} ºC."
        )

        while not stop_event.is_set():
            phase = current_phase()

            if phase not in ('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'EVAPORATION', 'WAIT_SHUTTER_CLOSE'):
                reset_temperature_pid(f'Phase {phase} is not PID-controlled')
                time.sleep(0.5)
                continue

            if not keysight_state['automation_active']:
                time.sleep(0.5)
                continue

            current_temp = latest_ck1_temperature()
            ck1_rate_avg = average_ck1_rate()
            current_setpoint = keysight_state['set_current_a']
            if current_setpoint is None:
                current_setpoint = KEYSIGHT_START_CURRENT_A

            if current_setpoint >= KEYSIGHT_HARD_STOP_A:
                stop_keysight_output(
                    f'Hard current stop reached ({current_setpoint:.3f} A >= {KEYSIGHT_HARD_STOP_A:.3f} A)'
                )
                stop_event.set()
                time.sleep(1)
                continue

            if current_setpoint > KEYSIGHT_SOFT_WARNING_A:
                keysight_set_current(KEYSIGHT_SOFT_WARNING_A)
                current_setpoint = KEYSIGHT_SOFT_WARNING_A

            if current_setpoint >= KEYSIGHT_SOFT_WARNING_A - 1e-9:
                now = time.time()
                if now - keysight_state.get('last_soft_cap_warning_at', 0.0) >= 15.0:
                    ts, formatted, dec = log_timestamp()
                    print(
                        f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                        f"{Fore.YELLOW}Soft current cap active: holding at "
                        f"{current_setpoint:.3f} A. Hard stop remains "
                        f"{KEYSIGHT_HARD_STOP_A:.3f} A.{Style.RESET_ALL}"
                    )
                    keysight_state['last_soft_cap_warning_at'] = now

            if manual_current_is_enabled():
                now = time.time()
                with manual_current_lock:
                    last_log = manual_current_state.get('last_hold_log_at', 0.0)
                    if now - last_log >= 15.0:
                        manual_current_state['last_hold_log_at'] = now
                        should_log_manual_hold = True
                    else:
                        should_log_manual_hold = False

                if should_log_manual_hold:
                    ts, formatted, dec = log_timestamp()
                    print(
                        f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
                        f"{Fore.YELLOW}Manual Keysight current control is active: "
                        f"automation Steps/Slope/PID current corrections are paused; "
                        f"holding software setpoint at {keysight_state.get('set_current_a')} A."
                        f"{Style.RESET_ALL}"
                    )
                keysight_state['last_step_at'] = now
                time.sleep(0.5)
                continue

            now = time.time()
            if now - keysight_state['last_step_at'] < PID_CONTROL_PERIOD_S:
                time.sleep(0.5)
                continue

            # After the heating target has been reached, keep using PID during
            # shutter waiting and dp_dbba_evaporation. Do not freeze the current in HOLD.
            if phase != 'HEATING_UP':
                apply_temperature_pid_control(current_temp, current_setpoint)
                time.sleep(0.5)
                continue

            # During HEATING_UP, use the chosen ramp-up strategy only while we are
            # clearly below the target band. Around the target, PID takes over.
            if should_use_temperature_pid(current_temp):
                apply_temperature_pid_control(current_temp, current_setpoint)
                time.sleep(0.5)
                continue

            # If the shutter condition is already satisfied, process_controller will
            # switch phase. The automation only keeps the present current briefly.
            if heating_ready_for_shutter(current_temp, ck1_rate_avg):
                time.sleep(0.5)
                continue

            maybe_auto_switch_steps_to_slope(current_temp)
            step_period_s = current_ramp_step_period_s(current_temp, current_setpoint)
            if now - keysight_state['last_step_at'] < step_period_s:
                time.sleep(0.5)
                continue

            if should_use_steps_ramp(current_temp):
                settings = get_live_ramp_settings()
                nudge_keysight_current(
                    +KEYSIGHT_STEP_A,
                    (
                        f'Steps ramp up mode: fixed step; CK-1 temp '
                        f'{current_temp if current_temp is not None else "--"} ºC '
                        f'< {settings["steps_until_temp_c"]:.1f} ºC'
                    ),
                    max_current=KEYSIGHT_SOFT_WARNING_A,
                )
            else:
                apply_temperature_slope_control(current_setpoint)

            keysight_state['last_step_at'] = time.time()
            time.sleep(0.5)

    except Exception as e:
        print(f'Error in Keysight automation: {e}')
        stop_event.set()

def process_controller():
    print_banner(
        "Process phases loaded: HEATING_UP -> WAIT_SHUTTER_OPEN -> EVAPORATION -> WAIT_SHUTTER_CLOSE -> FINISHED/HANDOFF\n"
        "DP-DBBA uses the same live PID/watchdog/Keysight protections as Heat up + Calibration v7.3.\n"
        "Use the GUI buttons for Open Shutter, Close Shutter, and Abort ramp-down. "
        "Terminal shortcuts still work: 'o' open, 'c' close, 'i' values, 'h' targets, 'q' stop."
    )

    while not stop_event.is_set():
        phase = current_phase()
        ck1_temp = latest_ck1_temperature()
        ck1_thickness = latest_ck1_thickness()
        sample_thickness = latest_sample_thickness()

        ck1_rate_avg = average_ck1_rate()

        now = time.time()
        if now - process_state['last_status_print'] >= 15:
            rel_sample = relative_sample_thickness()
            rel_ck1 = relative_ck1_thickness()
            print(
                f"[STATUS] phase={phase} | CK1 temp={ck1_temp} ºC | CK1 thick={ck1_thickness} Å | "
                f"CK1 rate avg={ck1_rate_avg} Å/s | Sample thick={sample_thickness} Å | "
                f"CK1 rel={rel_ck1} Å | Sample rel={rel_sample} Å | Iset={keysight_state['set_current_a']} A | "
                f"shutter_closed={process_state.get('shutter_close_confirmed', False)}"
            )
            process_state['last_status_print'] = now

        if phase == 'HEATING_UP':
            trigger_reason = None
            if heating_ready_for_shutter(ck1_temp, ck1_rate_avg):
                rate_high = get_ck1_rate_high_a_per_s()
                trigger_reason = (
                    f'CK-1 temperature reached {ck1_temp:.1f} ºC and '
                    f'average CK-1 rate reached the target '
                    f'>= {get_ck1_rate_target_a_per_s():.2f} Å/s '
                    f'(measured {ck1_rate_avg:.2f} Å/s over the last {CK1_RATE_AVG_WINDOW_POINTS} points; '
                    f'upper band value {rate_high:.2f} Å/s is not blocking)'
                )

            if trigger_reason is not None:
                reset_temperature_pid('Heating target reached; PID hold continues')
                request_snapshot('heating_end')
                save_phase_summary('heating_end')
                with state_lock:
                    # DP-DBBA relative evaporation values start only after Open Shutter confirmation.
                    process_state['baseline_ck1_thickness'] = None
                    process_state['baseline_sample_thickness'] = None
                    process_state['plots_reset_for_evaporation'] = False
                    process_state['snapshot_taken'] = True
                    process_state['shutter_open_confirmed'] = False
                    process_state['shutter_close_confirmed'] = False
                set_phase('WAIT_SHUTTER_OPEN', trigger_reason)
                print_banner(
                    f"Heating phase finished: {trigger_reason}\n"
                    f"Temperature PID remains active; current is not frozen in HOLD.\n"
                    f"NOW OPEN THE SHUTTER and click the Open Shutter button.\n"
                    f"Target CK-1 thickness for DP-DBBA = {TARGET_CK1_THICKNESS_A:.2f} Å"
                )

        elif phase == 'WAIT_SHUTTER_OPEN':
            if process_state['shutter_open_confirmed']:
                reset_evaporation_measurement_window()
                with state_lock:
                    process_state['shutter_close_confirmed'] = False
                set_phase('EVAPORATION', 'User confirmed shutter open')
                print_banner(
                    "DP-DBBA evaporation phase started. QMB evaporation window was reset at shutter opening. "
                    "Temperature PID remains active."
                )

        elif phase == 'EVAPORATION':
            ck1_rel = relative_ck1_thickness()
            if ck1_rel is not None and ck1_rel >= EVAPORATION_TARGET_CK1_A:
                request_snapshot('dp_dbba_target_reached')
                save_phase_summary('dp_dbba_target_reached')

                with state_lock:
                    # Force a fresh close confirmation after the target-reached warning.
                    process_state['shutter_close_confirmed'] = False
                    process_state['shutter_open_confirmed'] = False

                set_phase(
                    'WAIT_SHUTTER_CLOSE',
                    f'CK-1 target thickness reached: {ck1_rel:.2f} Å; waiting for shutter close'
                )
                print_banner(
                    f"TARGET REACHED: CK-1 relative thickness = {ck1_rel:.2f} Å\n"
                    f"Ideal CK-1 evaporation thickness = {IDEAL_CK1_EVAPORATION_THICKNESS_A:.2f} Å\n"
                    f"Real Sample Thickness = {REAL_SAMPLE_THICKNESS_A:.2f} Å\n"
                    f"PHASE: CLOSE THE SHUTTER\n"
                    f"Click the Close Shutter button. The script will finish without ramp-down and leave the Keysight ON at base current for script 4."
                )
                _set_live_action_status('CLOSE THE SHUTTER NOW | Click Close Shutter to finish and hand off to script 4')

        elif phase == 'WAIT_SHUTTER_CLOSE':
            if process_state.get('shutter_close_confirmed', False):
                held_current = hold_keysight_for_next_script(
                    'Shutter closed confirmed; DP-DBBA finished; handoff to script 4'
                )
                set_phase('FINISHED', 'Shutter closed confirmed; Keysight left ON for script 4')
                request_snapshot('finished_handoff_to_script4')
                save_phase_summary('finished_handoff_to_script4')
                print_banner(
                    "SHUTTER CLOSE CONFIRMED.\n"
                    "DP-DBBA is finished. No ramp-down was performed in this script.\n"
                    f"Keysight is left ON at base current {held_current:.3f} A.\n"
                    "Now start script 4 to continue with NPG Annealings / controlled ramp-down."
                )
                stop_event.set()

        elif phase == 'ABORT_RAMP_DOWN':
            time.sleep(0.5)

        elif phase == 'SAFETY_STOP':
            time.sleep(0.2)

        elif phase == 'FINISHED':
            time.sleep(1)

        time.sleep(0.5)

def current_run_values_text():
    return (
        f"Input thickness ratio = {input_thickness_ratio:.6f}\n"
        f"Ideal CK-1 evaporation thickness = {IDEAL_CK1_EVAPORATION_THICKNESS_A:.2f} Å\n"
        f"Real Sample Thickness = {REAL_SAMPLE_THICKNESS_A:.2f} Å\n"
        f"Target CK-1 thickness = {TARGET_CK1_THICKNESS_A:.2f} Å\n"
        f"Current CK-1 relative thickness = {relative_ck1_thickness() if relative_ck1_thickness() is not None else '--'} Å\n"
        f"Current Sample relative thickness = {relative_sample_thickness() if relative_sample_thickness() is not None else '--'} Å"
    )


def user_command_listener():
    while not stop_event.is_set():
        try:
            command = input().strip().lower()
        except EOFError:
            break
        except Exception:
            time.sleep(0.2)
            continue

        if command == 'o':
            confirm_shutter_open('terminal command')
        elif command == 'c':
            confirm_shutter_closed('terminal command')
        elif command == 'q':
            print_banner("Manual stop requested by user.")
            emergency_keysight_shutdown('Manual stop requested by user')
            break
        elif command == 's':
            request_snapshot('manual')
            save_phase_summary('manual')
        elif command == 'i':
            print(current_run_values_text())
        elif command == 'h':
            print(get_live_heating_targets())
        elif command:
            print("Commands: 'o' = shutter open, 'c' = shutter closed, 's' = save snapshot, 'i' = show DP-DBBA values, 'h' = show live targets, 'q' = stop")

# _____________________MAIN_______________________________________________________
def main():
    set_oven_pid_setpoint(OVEN_TARGET_TEMPERATURE_C)

    threads = [
        threading.Thread(target=monitor_qmb, daemon=True),
        threading.Thread(target=read_pressure, daemon=True),
        threading.Thread(target=read_PID, daemon=True),
        threading.Thread(target=read_pyrometer, daemon=True),
        threading.Thread(target=read_arduino, daemon=True),
        threading.Thread(target=read_powersupply, daemon=True),
        threading.Thread(target=automate_keysight_heating, daemon=True),
        threading.Thread(target=temperature_safety_watchdog, daemon=True),
        threading.Thread(target=process_controller, daemon=True),
        threading.Thread(target=user_command_listener, daemon=True),
    ]

    for thread in threads:
        thread.start()

    setup_live_target_controls()
    setup_temperature_view_selector()
    show_live_plot_window()

    last_plot_refresh_at = 0.0
    last_data_save_at = 0.0

    try:
        while not stop_event.is_set():
            now = time.time()

            if now - last_plot_refresh_at >= GUI_REFRESH_INTERVAL_S:
                snapshot = copy_data_snapshot()
                update_live_plot(snapshot)
                last_plot_refresh_at = now

            if now - last_data_save_at >= DATA_SAVE_INTERVAL_S:
                snapshot = copy_data_snapshot()
                write_data_files(snapshot)
                last_data_save_at = now

            process_pending_snapshots()
            safe_live_plot_refresh(0.01)
    except KeyboardInterrupt:
        print('(▀̿Ĺ̯▀̿ ̿) Stop all the threads!!!')
        emergency_keysight_shutdown('KeyboardInterrupt / manual stop')
    except Exception as e:
        print(f'Fatal error in main loop: {e}')
        emergency_keysight_shutdown(f'Fatal error in main loop: {e}')
        raise
    finally:
        stop_event.set()
        process_pending_snapshots()

    for thread in threads:
        if thread.is_alive():
            try:
                thread.join(timeout=2)
            except KeyboardInterrupt:
                emergency_keysight_shutdown('KeyboardInterrupt during thread join')
                break

    if normal_completion_leaves_keysight_on():
        leave_keysight_on_message('Main cleanup before final plots')
    else:
        force_keysight_zero_output('Main cleanup before final plots')
    print('> All threads stopped.')
    print(f"Input thickness ratio: {input_thickness_ratio:.6f}")
    print(f"Ideal CK-1 evaporation thickness: {IDEAL_CK1_EVAPORATION_THICKNESS_A:.2f} Å")
    print(f"Real Sample Thickness: {REAL_SAMPLE_THICKNESS_A:.2f} Å")
    print(f"Oven PID target temperature: {OVEN_TARGET_TEMPERATURE_C:.1f} ºC")

    fig2, ((fax_thickness_ck1, fax_rate_ck1, fax_pressure_xgs600),
           (fax_thickness_sample, fax_rate_sample, fax_temperature_oven),
           (fax_current_keysight, fax_voltage_keysight, fax_temperature_ck1)) = plt.subplots(3, 3, figsize=(18, 15))

    fig2.suptitle('DP-DBBA Evaporation parameters', fontsize=16, fontweight='bold')
    plt.subplots_adjust(left=0.05, right=0.99, top=0.9, bottom=0.1, hspace=0.45, wspace=0.25)

    fax_thickness_ck1.plot(data['CK-1 evaporator QMB']['thickness_times'], data['CK-1 evaporator QMB']['thickness_data'], '-o', color='green', markersize=4)
    fax_thickness_ck1.set_title('CK-1 Evaporator QMB Thickness'); fax_thickness_ck1.set_xlabel(''); fax_thickness_ck1.set_ylabel('Thickness (Å)'); fax_thickness_ck1.tick_params(axis='x', rotation=30)
    fax_rate_ck1.plot(data['CK-1 evaporator QMB']['rate_times'], data['CK-1 evaporator QMB']['rate_data'], '-o', color='green', markersize=4)
    fax_rate_ck1.set_title('CK-1 Evaporator QMB Rate'); fax_rate_ck1.set_xlabel(''); fax_rate_ck1.set_ylabel('Rate (Å/s)'); fax_rate_ck1.tick_params(axis='x', rotation=30)
    fax_thickness_sample.plot(data['Sample QMB']['thickness_times'], data['Sample QMB']['thickness_data'], '-o', color='green', markersize=4)
    fax_thickness_sample.set_title('Sample QMB Thickness'); fax_thickness_sample.set_xlabel(''); fax_thickness_sample.set_ylabel('Thickness (Å)'); fax_thickness_sample.tick_params(axis='x', rotation=30)
    fax_rate_sample.plot(data['Sample QMB']['rate_times'], data['Sample QMB']['rate_data'], '-o', color='green', markersize=4)
    fax_rate_sample.set_title('Sample QMB Rate'); fax_rate_sample.set_xlabel(''); fax_rate_sample.set_ylabel('Rate (Å/s)'); fax_rate_sample.tick_params(axis='x', rotation=30)
    fax_pressure_xgs600.plot(data['XGS600 HFIG pressure']['pressure_times'], data['XGS600 HFIG pressure']['pressure_data'], '-o', color='blue', markersize=4)
    fax_pressure_xgs600.set_title('XGS600 HFIG Pressure'); fax_pressure_xgs600.set_xlabel(''); fax_pressure_xgs600.set_ylabel('Pressure (mbar)'); fax_pressure_xgs600.tick_params(axis='x', rotation=30)
    fax_temperature_oven.plot(data['Oven PID temperature']['temperature_times'], data['Oven PID temperature']['temperature_data'], '-', color='magenta', linewidth=1.7, label='Oven PID')
    fax_temperature_oven.plot(data['IMPAC pyrometer']['temperature_times'], data['IMPAC pyrometer']['temperature_data'], '-', color='deepskyblue', linewidth=1.7, label='Pyrometer raw')
    fax_temperature_oven.plot(data['IMPAC pyrometer']['temperature_times'], data['IMPAC pyrometer']['sample_temperature_data'], '-', color='darkorange', linewidth=1.7, label='Sample estimate')
    fax_temperature_oven.set_title('Temperature Comparison'); fax_temperature_oven.set_xlabel(''); fax_temperature_oven.set_ylabel('Temperature (ºC)'); fax_temperature_oven.tick_params(axis='x', rotation=30); fax_temperature_oven.legend(loc='best', fontsize=8)
    fax_current_keysight.plot(data['Keysight power supply']['current_times'], data['Keysight power supply']['current_data'], '-o', color='goldenrod', markersize=4)
    fax_current_keysight.set_title('Keysight Power Supply Current'); fax_current_keysight.set_xlabel('Time'); fax_current_keysight.set_ylabel('Current (A)'); fax_current_keysight.tick_params(axis='x', rotation=30)
    fax_voltage_keysight.plot(data['Keysight power supply']['voltage_times'], data['Keysight power supply']['voltage_data'], '-o', color='goldenrod', markersize=4)
    fax_voltage_keysight.set_title('Keysight Power Supply Voltage'); fax_voltage_keysight.set_xlabel('Time'); fax_voltage_keysight.set_ylabel('Voltage (V)'); fax_voltage_keysight.tick_params(axis='x', rotation=30)
    fax_temperature_ck1.plot(data['Arduino CK-1 crucible temperature']['temperature_times'], data['Arduino CK-1 crucible temperature']['temperature_data'], '-o', color='red', markersize=4)
    fax_temperature_ck1.set_title('Arduino CK-1 Crucible Temperature'); fax_temperature_ck1.set_xlabel('Time'); fax_temperature_ck1.set_ylabel('Temperature (ºC)'); fax_temperature_ck1.tick_params(axis='x', rotation=30)

    plot_file_name = f"{sample_name} final DP-DBBA evaporation plot.png"
    plot_file_path = os.path.join(final_folder_path, plot_file_name)
    fig2.savefig(plot_file_path)
    print(f"All data has been saved in the folder '{final_folder_path}'")
    try:
        if AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED:
            plt.close('all')
        else:
            print('Waiting for you to close the final plot windows...')
            plt.show()
    except KeyboardInterrupt:
        emergency_keysight_shutdown('KeyboardInterrupt while showing final plots')
    finally:
        if normal_completion_leaves_keysight_on():
            leave_keysight_on_message('Program finalization')
        else:
            force_keysight_zero_output('Program finalization')
        close_all_serial_connections('Program finalization')
    print('Script finalized.')

def _failsafe_on_exit():
    if not keysight_should_remain_on_after_stop():
        emergency_keysight_shutdown('Python exit failsafe')
    close_all_serial_connections('Python exit failsafe')


def _sigint_handler(signum, frame):
    emergency_keysight_shutdown('SIGINT / Ctrl+C')
    raise KeyboardInterrupt


try:
    signal.signal(signal.SIGINT, _sigint_handler)
except Exception:
    pass


atexit.register(_failsafe_on_exit)


if __name__ == '__main__':
    main()
