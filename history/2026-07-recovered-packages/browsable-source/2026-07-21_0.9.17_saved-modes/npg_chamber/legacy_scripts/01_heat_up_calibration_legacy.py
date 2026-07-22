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

RUN_AUTOMATION_OVERRIDES = load_phase_overrides("heat")

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

sample_name = os.environ.get("NPG_CHAMBER_RUN_NAME", "").strip() or input("Enter the name for this evaporation run: ")
AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED = os.environ.get("NPG_CHAMBER_UNIFIED_LAUNCHER", "").strip() == "1"

# _____________________SAVE THE DATA____________________________________________
# Save data next to this script, not in the terminal's current working directory.
script_folder = _resolve_phase_data_parent("Heat up + Calibration Data")
safe_sample_name = re.sub(r'[<>:"/\\|?*]+', '_', sample_name).strip() or 'unnamed_trial'
custom_folder_name = f"Heat up + Calibration data {safe_sample_name}"
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
WHAT_CHANGED_TEXT = """WHAT CHANGED IN THIS CLEAN VERSION
============================================================
This build starts again from the simpler Heat up + Calibration script and
keeps the automation logic explicit and easier to maintain. It also adds the
monitoring-only IMPAC IPE 140 profile, logging and three-way temperature view;
no pyrometer value is used by the heating, PID, Keysight or safety logic.

Main changes in this clean build:
1. The normal current ceiling is now KEYSIGHT_SOFT_WARNING_A = 0.670 A.
   All normal current commands are clamped there, including assist boost.
2. The old KEYSIGHT_MAX_CURRENT_A and KEYSIGHT_OCP_A duplication was removed.
   There is now a single hard current protection value:
      KEYSIGHT_HARD_STOP_A = 0.685 A
3. The Keysight hardware OCP latch is now deliberately above the software hard stop:
      KEYSIGHT_HARD_STOP_A       = 0.685 A  # software measured-current stop
      KEYSIGHT_INSTRUMENT_OCP_MARGIN_A = 0.005 A
      KEYSIGHT_INSTRUMENT_OCP_A        = round(KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A, 3)
   This avoids nuisance OCP trips from small transients/readback tolerances near 0.670 A.
4. The Keysight voltage setpoint limit, software emergency stop, and hardware OVP latch
   are now separate concepts:
      KEYSIGHT_VOLTAGE_LIMIT_V   = 2.30 V   # normal compliance limit
      KEYSIGHT_HARD_STOP_V       = 2.45 V   # software measured-voltage stop
      KEYSIGHT_INSTRUMENT_OVP_MARGIN_V = 0.05 V
      KEYSIGHT_INSTRUMENT_OVP_V        = KEYSIGHT_HARD_STOP_V + KEYSIGHT_INSTRUMENT_OVP_MARGIN_V
   The normal voltage limit no longer acts as the OVP trip level.
5. A selectable ramp-up mode is kept in the GUI:
      - Steps ramp up mode: fixed +0.005 A steps during the approach.
      - Slope ramp up mode: CK-1 temperature-slope controller during the approach.
   Once the CK-1 reaches the editable temperature target band, a PID temperature
   hold takes over and keeps regulating the Keysight current around that target.
6. The steps-ramp threshold temperature and step period can be edited live.
7. The CK-1 temperature and rate plots now autoscale from their measured data,
   so the early low-temperature/rate changes are visible instead of being
   compressed by distant target reference lines.
8. The calibration target reached message is now a proper WAIT_SHUTTER_CLOSE
   phase. RAMP_DOWN starts only after pressing Close Shutter.
9. The final thickness ratio is now calculated as:
      CK-1 relative thickness / Sample relative thickness
10. The heating-to-open-shutter transition no longer blocks when the CK-1
    average rate is above the upper rate band. It requires temperature target
    reached and average rate >= target rate.
11. Saved phase snapshots are now graph-only Matplotlib images. They do not
    include the side control panel, GUI buttons, desktop, or full screen.
12. A temperature watchdog was added as an independent safety layer above the
    PID. It can force current down near a thermal limit and hard-stop the
    Keysight if the CK-1 temperature, sensor freshness, or sensor values are unsafe.
13. Saved PNG plots now use the same line colours as the live interface.
14. Dependent setpoints are now expressed as formulas where appropriate:
    CK-1 rate low/high follow the editable target rate, and instrument
    OCP/OVP follow their corresponding software hard-stop values.
15. The right control-panel titles and Last action area were moved slightly
    upward to avoid visual collisions with the operator buttons.

Default working values in this version:
- KEYSIGHT_START_CURRENT_A = 0.005
- KEYSIGHT_BASE_WORK_CURRENT_A = 0.640
- KEYSIGHT_SOFT_WARNING_A = 0.670
- KEYSIGHT_HARD_STOP_A = 0.685
- KEYSIGHT_INSTRUMENT_OCP_MARGIN_A = 0.005
- KEYSIGHT_INSTRUMENT_OCP_A = 0.690
- KEYSIGHT_VOLTAGE_LIMIT_V = 2.30
- KEYSIGHT_HARD_STOP_V = 2.45
- KEYSIGHT_INSTRUMENT_OVP_MARGIN_V = 0.05
- KEYSIGHT_INSTRUMENT_OVP_V = 2.50
- STEPS_RAMP_UNTIL_TEMP_C = 100.0
- STEPS_RAMP_STEP_PERIOD_S = 15.0
- KEYSIGHT_STEP_A = 0.005
- HEATING_TRIGGER_TEMP_C = 242.0
- CK1_RATE_TARGET_A_PER_S = 0.40
- CK1_RATE_AVG_WINDOW_POINTS = 8
- CALIBRATION_TARGET_SAMPLE_A = 1.0
- TEMP_SLOPE_WINDOW_POINTS = 15
- TEMP_SLOPE_TARGET_EARLY_C_PER_MIN = 9.0
- TEMP_SLOPE_TARGET_MID_C_PER_MIN = 8.0
- TEMP_SLOPE_TARGET_LATE_C_PER_MIN = 7.0
- RAMPDOWN_STEP_PERIOD_S = 15

Notes for this build:
- The soft current cap is an active normal-operation cap, not just a warning.
- The old absolute-temperature ramp-down protection is replaced by an independent
  target-relative temperature watchdog.
- Temperature stabilization is handled by the PID hold around the editable target.
- The watchdog remains active even if the PID calculation fails or stops correcting.
- The software hard current/voltage values are still checked from live readback.
- The instrument OCP/OVP latch values include margin so the Keysight does not
  switch itself off from tiny transients just below the software thresholds.
- Review and tune PID values on safe test conditions before running on hardware.
"""


def show_what_changed():
    print("\n" + WHAT_CHANGED_TEXT + "\n")


show_what_changed()


init()
stop_event = threading.Event()
data_lock = threading.Lock()
keysight_lock = threading.Lock()
state_lock = threading.Lock()

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
TEMP_WATCHDOG_SOFT_COOLDOWN_S = 0.50     # forced current reduction per hard action
TEMP_WATCHDOG_SENSOR_STALE_TIMEOUT_S = 180.0 # time that can pass without receiving inputs of the CK-1 Temp
TEMP_WATCHDOG_SENSOR_INITIAL_GRACE_S = 120.0 #initial time that can pass when opening the script without receiving inputs of the CK-1 Temp
TEMP_WATCHDOG_VALID_MIN_C = -20.0
TEMP_WATCHDOG_VALID_MAX_C = 500.0
TEMP_WATCHDOG_MAX_JUMP_C = 35.0         # reject sudden impossible jumps between reads
TEMP_WATCHDOG_ACTIVE_PHASES = ('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'CALIBRATION', 'WAIT_SHUTTER_CLOSE')

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


CALIBRATION_TARGET_SAMPLE_A = 1.0
RAMPDOWN_STEP_A = 0.010
RAMPDOWN_STEP_PERIOD_S = 15
RAMPDOWN_ZERO_THRESHOLD_A = 0.003

# Apply validated launcher values only inside this child process. The packaged
# defaults and source files are not modified. Hard safety limits are deliberately
# absent from the editable parameter schema.
apply_overrides_to_namespace("heat", globals(), RUN_AUTOMATION_OVERRIDES)
KEYSIGHT_INSTRUMENT_OCP_A = KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A
KEYSIGHT_INSTRUMENT_OVP_V = KEYSIGHT_HARD_STOP_V + KEYSIGHT_INSTRUMENT_OVP_MARGIN_V
CK1_RATE_LOW_A_PER_S = CK1_RATE_TARGET_A_PER_S - 0.05
CK1_RATE_HIGH_A_PER_S = CK1_RATE_TARGET_A_PER_S + 0.05
print("\n" + format_override_summary("heat", RUN_AUTOMATION_OVERRIDES) + "\n")
try:
    parameter_record_path = write_effective_parameters(
        os.path.join(final_folder_path, f"{safe_sample_name}_automation_parameters.json"),
        "heat",
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
    'final_thickness_ratio': None,
}

heating_targets_lock = threading.Lock()
live_heating_targets = {
    # Runtime values read by the automation logic. GUI edits update this dict;
    # functions below read from here instead of from the startup constants.
    'trigger_temp_c': HEATING_TRIGGER_TEMP_C,
    'rate_target_a_per_s': CK1_RATE_TARGET_A_PER_S,
    'rate_low_a_per_s': CK1_RATE_LOW_A_PER_S,
    'rate_high_a_per_s': CK1_RATE_HIGH_A_PER_S,
    'pid_temp_band_c': PID_TEMP_BAND_C,
}
DEFAULT_LIVE_HEATING_TARGETS = dict(live_heating_targets)

ramp_settings_lock = threading.Lock()
live_ramp_settings = {
    # Runtime ramp values read by the ramp controller. GUI edits update this dict.
    'mode': DEFAULT_RAMP_UP_MODE,
    'steps_until_temp_c': STEPS_RAMP_UNTIL_TEMP_C,
    'steps_step_period_s': STEPS_RAMP_STEP_PERIOD_S,
    'slope_early_c_per_min': TEMP_SLOPE_TARGET_EARLY_C_PER_MIN,
    'slope_mid_c_per_min': TEMP_SLOPE_TARGET_MID_C_PER_MIN,
    'slope_late_c_per_min': TEMP_SLOPE_TARGET_LATE_C_PER_MIN,
}
DEFAULT_LIVE_RAMP_SETTINGS = dict(live_ramp_settings)

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
      (ax_current_keysight, ax_voltage_keysight, ax_temperature_ck1)) = plt.subplots(3, 3, figsize=(19.2, 11.2))
fig.patch.set_facecolor('#f4f6f8')
# A larger vertical gap gives every graph title its own header space and keeps
# the temperature selector clear of the plots above and below it.
plt.subplots_adjust(left=0.052, right=0.705, top=0.875, bottom=0.075, hspace=0.68, wspace=0.30)
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


def get_pid_temp_band_c():
    return get_live_heating_targets()['pid_temp_band_c']


def ck1_rate_band_from_target(rate_target_a_per_s):
    """Return the CK-1 rate acceptance band derived from the target rate.

    The GUI exposes only the target rate. The low/high values are intentionally
    dependent variables, so changing the target automatically changes both band
    edges everywhere in the internal logic.
    The half-width is inferred from the startup values you set above, so your
    experimental values are not replaced by hidden hard-coded values here.
    """
    rate_target_a_per_s = float(rate_target_a_per_s)
    band_half_width = max(0.0, (CK1_RATE_HIGH_A_PER_S - CK1_RATE_LOW_A_PER_S) / 2.0)
    rate_low_a_per_s = max(0.0, rate_target_a_per_s - band_half_width)
    rate_high_a_per_s = rate_target_a_per_s + band_half_width
    return rate_low_a_per_s, rate_high_a_per_s


def refresh_live_target_lines():
    targets = get_live_heating_targets()
    live_target_temp_line.set_ydata([targets['trigger_temp_c'], targets['trigger_temp_c']])
    live_target_rate_line.set_ydata([targets['rate_target_a_per_s'], targets['rate_target_a_per_s']])
    live_target_rate_low_line.set_ydata([targets['rate_low_a_per_s'], targets['rate_low_a_per_s']])
    live_target_rate_high_line.set_ydata([targets['rate_high_a_per_s'], targets['rate_high_a_per_s']])


def set_live_heating_targets(trigger_temp_c, rate_target_a_per_s, pid_temp_band_c):
    rate_low_a_per_s, rate_high_a_per_s = ck1_rate_band_from_target(rate_target_a_per_s)

    with heating_targets_lock:
        live_heating_targets['trigger_temp_c'] = float(trigger_temp_c)
        live_heating_targets['rate_target_a_per_s'] = float(rate_target_a_per_s)
        live_heating_targets['rate_low_a_per_s'] = rate_low_a_per_s
        live_heating_targets['rate_high_a_per_s'] = rate_high_a_per_s
        live_heating_targets['pid_temp_band_c'] = max(0.1, float(pid_temp_band_c))

    refresh_live_target_lines()

    message = (
        f"Live heating targets updated | T={float(trigger_temp_c):.1f} ºC | "
        f"rate target={float(rate_target_a_per_s):.3f} Å/s | "
        f"rate band=[{rate_low_a_per_s:.3f}, {rate_high_a_per_s:.3f}] Å/s | "
        f"PID T band=±{max(0.1, float(pid_temp_band_c)):.2f} ºC"
    )

    print_banner(message)
    if live_target_status_text is not None:
        _set_live_action_status(message)

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
        if live_target_status_text is not None:
            _set_live_action_status(message)
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
    if live_target_status_text is not None:
        _set_live_action_status(message)
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
    if live_target_status_text is not None:
        _set_live_action_status(message.replace('. ', '.\n'))
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
            _set_live_action_status(message)
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

def _wrap_gui_column_text(text: str, max_chars: int = 20) -> str:
    wrapped_lines = []
    for source_line in str(text).splitlines():
        words = source_line.split()
        if not words:
            wrapped_lines.append('')
            continue

        line = words[0]
        for word in words[1:]:
            if len(line) + 1 + len(word) <= max_chars:
                line += ' ' + word
            else:
                wrapped_lines.append(line)
                line = word
        wrapped_lines.append(line)
    return '\n'.join(wrapped_lines)


def _set_live_action_status(message: str):
    if live_target_status_text is not None:
        live_target_status_text.set_text(
            _wrap_gui_column_text(str(message).replace(' | ', '\n'))
        )
    try:
        fig.canvas.draw_idle()
    except Exception:
        pass


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
    refresh_manual_current_button_styles()


def _resume_automatic_current_control(event=None):
    set_manual_current_enabled(False, 'GUI Auto current button')


def confirm_shutter_open(source: str = 'GUI button'):
    ck1_baseline = latest_ck1_thickness()
    sample_baseline = latest_sample_thickness()
    with state_lock:
        process_state['baseline_ck1_thickness'] = ck1_baseline
        process_state['baseline_sample_thickness'] = sample_baseline
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


def request_gui_abort(event=None):
    message = 'Abort requested from GUI. Stopping automation and switching Keysight output off safely.'
    print_banner(message)
    _set_live_action_status(message)

    with state_lock:
        process_state['phase'] = 'SAFETY_STOP'
        process_state['transition_reason'] = message
        process_state['gui_auto_close'] = True

    try:
        request_snapshot('gui_abort')
        save_phase_summary('gui_abort')
    except Exception as e:
        print(f"Could not save GUI abort summary before stopping: {e}")

    emergency_keysight_shutdown('GUI Abort button')


def request_gui_finish(event=None):
    message = 'Finish requested from GUI. Entering RAMP_DOWN; Keysight output will switch OFF after reaching 0 A.'
    print_banner(message)
    _set_live_action_status(message)

    with state_lock:
        process_state['phase'] = 'RAMP_DOWN'
        process_state['phase_started_at'] = time.time()
        process_state['transition_reason'] = message
        process_state['rampdown_started'] = False
        process_state['gui_auto_close'] = True

    try:
        request_snapshot('gui_finish_ramp_down_start')
        save_phase_summary('gui_finish_ramp_down_start')
    except Exception as e:
        print(f"Could not save GUI finish summary before rampdown: {e}")


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
    ax_temperature_oven.set_title(_temperature_view_label(mode), fontsize=10.4, fontweight='bold', color=AXIS_ACCENTS['temperature'], pad=28)
    _refresh_temperature_view_button_styles()
    try:
        fig.canvas.draw_idle()
    except Exception:
        pass
    print(f"Temperature graph view changed to: {_temperature_view_label(mode)}")


def setup_temperature_view_selector():
    """Place the three-way selector in a dedicated header directly above its graph."""

    if temperature_view_buttons:
        return

    # Reserve a slim header inside the temperature subplot cell. The graph title
    # stays above this strip, while the buttons sit immediately next to the plot
    # they control and cannot collide with the plots in the neighbouring rows.
    original_bbox = ax_temperature_oven.get_position()
    selector_height = 0.020
    selector_gap = 0.004
    graph_header_height = selector_height + selector_gap
    ax_temperature_oven.set_position([
        original_bbox.x0,
        original_bbox.y0,
        original_bbox.width,
        original_bbox.height - graph_header_height,
    ])
    bbox = ax_temperature_oven.get_position()

    gap = 0.003
    total_width = bbox.width * 0.94
    x0 = bbox.x0 + (bbox.width - total_width) / 2.0
    button_width = (total_width - 2 * gap) / 3.0
    y = bbox.y1 + selector_gap
    options = (
        ('oven', 'OVEN PID'),
        ('pyrometer', 'PYROMETER'),
        ('sample', 'SAMPLE EST.'),
    )
    for index, (mode, label) in enumerate(options):
        button_ax = fig.add_axes([x0 + index * (button_width + gap), y, button_width, selector_height])
        button = Button(button_ax, label, color='#edf1f6', hovercolor='#ffffff')
        button.label.set_fontsize(7.8)
        button.label.set_fontweight('bold')
        button.on_clicked(lambda _event, selected=mode: set_temperature_view(selected))
        temperature_view_buttons[mode] = button
    set_temperature_view(temperature_view_state.get('mode', 'oven'))


def setup_live_target_controls():
    global live_target_status_text, live_dashboard_text, control_panel_ax

    panel_left = 0.725
    panel_bottom = 0.035
    panel_width = 0.260
    panel_height = 0.900

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
    add_panel_card(control_panel_ax, 0.025, 0.155, 0.950, 0.110, facecolor='#eefbf3', edgecolor='#bbf7d0')
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

    panel_text(0.05, 0.985, 'Heat up + Calibration', fontsize=12.2, color='#0f172a', weight='bold')
    panel_text(0.05, 0.957, 'Live controls and run status', fontsize=8.1, color='#475569')

    # Editable heating targets
    panel_text(0.05, 0.920, 'Editable heating targets', fontsize=9.3, color='#334155', weight='bold')
    target_box_specs = [
        ('trigger_temp_c', 'Temp target (ºC)', f"{HEATING_TRIGGER_TEMP_C:.1f}", 0.850),
        ('rate_target_a_per_s', 'Target CK-1 rate (Å/s)', f"{CK1_RATE_TARGET_A_PER_S:.3f}", 0.800),
        ('pid_temp_band_c', 'PID band (ºC)', f"{PID_TEMP_BAND_C:.2f}", 0.750),
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
    panel_text(0.05, 0.258, 'Operator controls', fontsize=9.6, color='#334155', weight='bold')
    btn_open = add_button(0.06, 0.213, 0.40, 0.036, 'Open shutter', '#dcfce7', '#bbf7d0', '#166534', fontsize=6.6)
    btn_open.on_clicked(_gui_open_shutter)
    live_target_buttons['open_shutter'] = btn_open

    btn_close = add_button(0.52, 0.213, 0.40, 0.036, 'Close shutter', '#ffedd5', '#fed7aa', '#9a3412', fontsize=6.6)
    btn_close.on_clicked(_gui_close_shutter)
    live_target_buttons['close_shutter'] = btn_close

    btn_abort = add_button(0.06, 0.172, 0.40, 0.036, 'Abort / safe stop', '#fee2e2', '#fecaca', '#991b1b', fontsize=7.8)
    btn_abort.on_clicked(request_gui_abort)
    live_target_buttons['abort'] = btn_abort

    btn_finish = add_button(0.52, 0.172, 0.40, 0.036, 'Finish phase', '#e0f2fe', '#bae6fd', '#075985', fontsize=7.8)
    btn_finish.on_clicked(request_gui_finish)
    live_target_buttons['finish'] = btn_finish

    # Run status and last action
    panel_text(0.05, 0.145, 'Process status', fontsize=9.3, color='#334155', weight='bold')
    live_dashboard_text = control_panel_ax.text(
        0.05, 0.128, '', transform=control_panel_ax.transAxes,
        fontsize=6.35, color='#0f172a', va='top', ha='left', linespacing=0.88,
        clip_on=True
    )

    panel_text(0.55, 0.145, 'Last action', fontsize=9.3, color='#334155', weight='bold')
    live_target_status_text = control_panel_ax.text(
        0.55, 0.128, '', transform=control_panel_ax.transAxes,
        fontsize=6.25, color='#334155', va='top', ha='left', linespacing=0.88,
        clip_on=True
    )

    set_live_heating_targets(
        HEATING_TRIGGER_TEMP_C,
        CK1_RATE_TARGET_A_PER_S,
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
            manager.set_window_title('Phase 01 · Heat up + Calibration')
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
    baseline = process_state['baseline_sample_thickness']
    current = latest_sample_thickness()
    if baseline is None or current is None:
        return None
    return current - baseline


def relative_ck1_thickness():
    baseline = process_state['baseline_ck1_thickness']
    current = latest_ck1_thickness()
    if baseline is None or current is None:
        return None
    return current - baseline



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


def emergency_keysight_shutdown(reason: str = 'Emergency shutdown'):
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
    title = f"Heat up + Calibration snapshot: {tag}"
    ratio = process_state.get('final_thickness_ratio')
    live_ratio = calculate_thickness_ratio()
    if ratio is None and live_ratio is not None:
        ratio = live_ratio
    if ratio is not None:
        title += f" | thickness ratio = {ratio:.3f}"
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


def calculate_thickness_ratio():
    """Return CK-1 relative thickness divided by Sample relative thickness."""
    ck1_rel = relative_ck1_thickness()
    sample_rel = relative_sample_thickness()

    if ck1_rel is None or sample_rel is None or sample_rel == 0:
        return None
    return ck1_rel / sample_rel


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
            f.write(f"thickness_ratio: {calculate_thickness_ratio()}\n")
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
    """
    Safely ramp the evaporator current down before switching the Keysight
    output off.

    Normal completion path:
      1) keep output ON,
      2) reduce current by RAMPDOWN_STEP_A every RAMPDOWN_STEP_PERIOD_S,
      3) only after reaching ~0 A, send OUTP OFF.

    Returns True if the rampdown completed normally.
    Returns False if it was interrupted by stop_event.
    """
    current = keysight_state.get('set_current_a')
    if current is None:
        current = latest_value('Keysight power supply', 'current_data') or 0.0
    current = max(0.0, float(current))

    keysight_state['automation_active'] = False
    keysight_state['hold_current_a'] = current
    keysight_state['reason_stopped'] = reason

    print_banner(
        f"PHASE: RAMP DOWN\n"
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
            stop_keysight_output('Ramp down interrupted by stop request')
            return False

        next_current = max(0.0, current - RAMPDOWN_STEP_A)
        if next_current <= RAMPDOWN_ZERO_THRESHOLD_A:
            next_current = 0.0

        keysight_set_current(next_current)
        keysight_state['hold_current_a'] = next_current

        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.YELLOW}RAMP DOWN: Keysight current {current:.3f} A -> "
            f"{next_current:.3f} A{Style.RESET_ALL}"
        )

        current = next_current
        if current <= RAMPDOWN_ZERO_THRESHOLD_A:
            break

        # Sleep in short chunks so Abort / Finish or Ctrl+C can interrupt safely.
        deadline = time.time() + RAMPDOWN_STEP_PERIOD_S
        while time.time() < deadline:
            if stop_event.is_set():
                stop_keysight_output('Ramp down interrupted by stop request')
                return False
            time.sleep(min(0.5, deadline - time.time()))

    try:
        keysight_set_current(0.0)
    except Exception as e:
        print(f"Could not send final CURR 0.000 command before output OFF: {e}")

    stop_keysight_output(reason + ' - rampdown complete at 0 A')
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
        'CALIBRATION': 'CALIBRATION',
        'WAIT_SHUTTER_CLOSE': 'CLOSE THE SHUTTER',
        'RAMP_DOWN': 'RAMP DOWN',
        'FINISHED': 'FINISHED',
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

    ck1_th_last = ck1_thickness_values[-1] if ck1_thickness_values else None
    ck1_rate_last = ck1_rate_values[-1] if ck1_rate_values else None
    sample_th_last = sample_thickness_values[-1] if sample_thickness_values else None
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
                                ts, formatted, dec = log_timestamp()
                                with data_lock:
                                    data[key]['thickness_times'].append(ts)
                                    data[key]['thickness_data'].append(thickness_value)
                                print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.GREEN}{key} Thickness: {thickness_value} Å{Style.RESET_ALL}")
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
                and expected_current > RAMPDOWN_ZERO_THRESHOLD_A
                and current_phase() not in ('RAMP_DOWN', 'FINISHED', 'SAFETY_STOP')
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
        keysight_write('OUTP OFF')
    except Exception:
        pass
    connections[key].close()



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
            command_EOT = chr(4)
            connections[key].write(command_EOT.encode())
            time.sleep(0.5)
            identifier_PV = "M1"
            command_read_PV = "00" + identifier_PV + chr(5)
            connections[key].write(command_read_PV.encode())
            time.sleep(0.1)
            PID_message = connections[key].read(connections[key].in_waiting or 100)
            PID_message_str = PID_message.decode(errors='ignore')
            temperature_str = PID_message_str[6:9]
            temperature_value = float(temperature_str)
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
            f"Temperature PID then keeps CK-1 around target ±{get_pid_temp_band_c():.2f} ºC.\n"
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

            if phase not in ('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'CALIBRATION', 'WAIT_SHUTTER_CLOSE'):
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
            # shutter waiting and calibration. Do not freeze the current in HOLD.
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
        "Process phases loaded: HEATING_UP -> WAIT_SHUTTER_OPEN -> CALIBRATION -> WAIT_SHUTTER_CLOSE -> RAMP_DOWN -> FINISHED\n"
        "Use the GUI buttons for Open Shutter, Close Shutter, Abort, and Finish. "
        "Terminal shortcuts still work: 'o' open, 'c' close, 'r' ratio, 'h' targets, 'q' stop."
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
            print(
                f"[STATUS] phase={phase} | CK1 temp={ck1_temp} ºC | CK1 thick={ck1_thickness} Å | "
                f"CK1 rate avg={ck1_rate_avg} Å/s | Sample thick={sample_thickness} Å | "
                f"Sample rel={rel_sample} Å | Iset={keysight_state['set_current_a']} A | "
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
                    process_state['snapshot_taken'] = True
                    process_state['shutter_open_confirmed'] = False
                    process_state['shutter_close_confirmed'] = False
                set_phase('WAIT_SHUTTER_OPEN', trigger_reason)
                print_banner(
                    f"Heating phase finished: {trigger_reason}\n"
                    f"Temperature PID remains active; current is not frozen in HOLD.\n"
                    f"NOW OPEN THE SHUTTER and click the Open Shutter button."
                )

        elif phase == 'WAIT_SHUTTER_OPEN':
            if process_state['shutter_open_confirmed']:
                with state_lock:
                    process_state['shutter_close_confirmed'] = False
                set_phase('CALIBRATION', 'User confirmed shutter open')
                print_banner("Calibration phase started. Temperature PID remains active.")
        elif phase == 'CALIBRATION':
            sample_rel = relative_sample_thickness()
            if sample_rel is not None and sample_rel >= CALIBRATION_TARGET_SAMPLE_A:
                thickness_ratio = calculate_thickness_ratio()
                process_state['final_thickness_ratio'] = thickness_ratio
                request_snapshot('calibration_end')
                save_phase_summary('calibration_end')

                ratio_message = 'Thickness ratio could not be calculated.'
                if thickness_ratio is not None:
                    ratio_message = (
                        f'Thickness ratio (CK-1 relative / Sample relative) = {thickness_ratio:.2f}'
                    )

                with state_lock:
                    # Force a fresh close confirmation after the target-reached warning.
                    process_state['shutter_close_confirmed'] = False
                    process_state['shutter_open_confirmed'] = False

                set_phase(
                    'WAIT_SHUTTER_CLOSE',
                    f'Sample relative thickness reached {sample_rel:.2f} Å; waiting for shutter close'
                )
                print_banner(
                    f"TARGET REACHED: Sample relative thickness = {sample_rel:.2f} Å\n"
                    f"{ratio_message}\n"
                    f"PHASE: CLOSE THE SHUTTER\n"
                    f"Click the Close Shutter button. The script will NOT enter RAMP_DOWN until this confirmation is received."
                )
                _set_live_action_status('CLOSE THE SHUTTER NOW | Click Close Shutter to continue to RAMP_DOWN')

        elif phase == 'WAIT_SHUTTER_CLOSE':
            if process_state.get('shutter_close_confirmed', False):
                set_phase('RAMP_DOWN', 'Shutter closed confirmed; starting safe rampdown')
                request_snapshot('ramp_down_start')
                save_phase_summary('ramp_down_start')
                print_banner(
                    "SHUTTER CLOSE CONFIRMED.\n"
                    "Starting RAMP_DOWN: evaporator current will decrease by 0.010 A every configured period until 0 A."
                )

                rampdown_completed = rampdown_keysight_output('Automatic safe rampdown after shutter close')

                if rampdown_completed:
                    set_phase('FINISHED', 'Safe rampdown complete after shutter close confirmation')
                    request_snapshot('finished_after_rampdown')
                    save_phase_summary('finished_after_rampdown')
                    print_banner("RAMP DOWN COMPLETE: Keysight current is 0 A and output is OFF. PHASE: FINISHED.")
                elif not stop_event.is_set():
                    print_banner(
                        "RAMP_DOWN did not complete, so FINISHED is blocked. "
                        "Check the Keysight current and use Abort / Finish only if you need to stop safely."
                    )

        elif phase == 'SAFETY_STOP':
            time.sleep(0.2)

        elif phase == 'RAMP_DOWN':
            if not process_state.get('rampdown_started', False):
                with state_lock:
                    process_state['rampdown_started'] = True

                rampdown_reason = process_state.get('transition_reason') or 'GUI Finish safe rampdown'
                rampdown_completed = rampdown_keysight_output(rampdown_reason)

                if rampdown_completed:
                    set_phase('FINISHED', 'Safe rampdown complete')
                    request_snapshot('finished_after_rampdown')
                    save_phase_summary('finished_after_rampdown')
                    print_banner("RAMP DOWN COMPLETE: Keysight current is 0 A and output is OFF. PHASE: FINISHED.")
                    if process_state.get('gui_auto_close', False):
                        stop_event.set()
                elif not stop_event.is_set():
                    print_banner(
                        "RAMP_DOWN did not complete, so FINISHED is blocked. "
                        "Check the Keysight current and use Abort if you need to stop immediately."
                    )
            else:
                time.sleep(0.2)

        elif phase == 'FINISHED':
            if AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED:
                with state_lock:
                    process_state['gui_auto_close'] = True
                stop_event.set()
            else:
                time.sleep(1)

        time.sleep(0.5)

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
        elif command == 'r':
            ratio = calculate_thickness_ratio()
            print(f"Thickness ratio (CK-1 relative / Sample relative): {ratio}")
        elif command == 'h':
            print(get_live_heating_targets())
        elif command:
            print("Commands: 'o' = shutter open, 'c' = shutter closed, 's' = save snapshot, 'r' = show thickness ratio, 'h' = show live targets, 'q' = stop")

# _____________________MAIN_______________________________________________________
def main():
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

    force_keysight_zero_output('Main cleanup before final plots')
    print('> All threads stopped.')
    final_ratio = process_state.get('final_thickness_ratio')
    if final_ratio is not None:
        print(f"Final thickness ratio (CK-1 relative / Sample relative): {final_ratio:.2f}")

    fig2, ((fax_thickness_ck1, fax_rate_ck1, fax_pressure_xgs600),
           (fax_thickness_sample, fax_rate_sample, fax_temperature_oven),
           (fax_current_keysight, fax_voltage_keysight, fax_temperature_ck1)) = plt.subplots(3, 3, figsize=(18, 15))

    fig2.suptitle('Heat up + Calibration parameters', fontsize=16, fontweight='bold')
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

    plot_file_name = f"{sample_name} final Heat up + Calibration plot.png"
    plot_file_path = os.path.join(final_folder_path, plot_file_name)
    fig2.savefig(plot_file_path)
    print(f"All data has been saved in the folder '{final_folder_path}'")
    try:
        if process_state.get('gui_auto_close', False):
            plt.close('all')
        else:
            print('Waiting for you to close the final plot windows...')
            plt.show()
    except KeyboardInterrupt:
        emergency_keysight_shutdown('KeyboardInterrupt while showing final plots')
    finally:
        force_keysight_zero_output('Program finalization')
        close_all_serial_connections('Program finalization')
    print('Script finalized.')

def _failsafe_on_exit():
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
