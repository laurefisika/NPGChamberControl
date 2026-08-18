import threading
import csv
import json
import serial
import time
from datetime import datetime, timedelta
import importlib.util
import os
import matplotlib

# Phase 01/03 use the fast Qt dashboard by default.  Setting
# NPG_CHAMBER_PHASE13_GUI=matplotlib keeps the previous GUI as a diagnostic
# fallback.  Matplotlib remains available off-screen for snapshots/final PNGs.
PHASE13_GUI_BACKEND = os.environ.get("NPG_CHAMBER_PHASE13_GUI", "qt").strip().lower()
USE_QT_PHASE13_DASHBOARD = (
    PHASE13_GUI_BACKEND not in {"matplotlib", "mpl", "legacy"}
    and importlib.util.find_spec("PySide6") is not None
    and importlib.util.find_spec("pyqtgraph") is not None
)
if USE_QT_PHASE13_DASHBOARD:
    matplotlib.use("Agg")
    plt = None
    TextBox = None
    Button = None
else:
    import matplotlib.pyplot as plt
    from matplotlib.widgets import TextBox, Button

import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from colorama import Fore, Style, init
import re
import math
import atexit
import signal

from npg_chamber.config.run_parameters import (
    apply_overrides_to_namespace,
    load_phase_overrides,
    load_pyrometer_settings,
    write_effective_parameters,
)
from npg_chamber.devices.pyrometer import ImpacIPE140, PyrometerProfile, PyrometerSerialConfig
from npg_chamber.common.pressure_alarm import PressureEmergencyAlarm
from npg_chamber.common.phase_dashboard_style import (
    AXIS_ACCENTS,
    add_panel_card,
    create_phase_badge,
    style_measurement_axis,
    update_phase_badge,
)
from npg_chamber.common.evaporation_control import (
    CONTROL_MODE_COMPOUND,
    CONTROL_MODE_RATE,
    CONTROL_MODE_TEMPERATURE,
    RatePidConfig,
    RatePidController,
    robust_rate_average,
)
from npg_chamber.common.professional_control import (
    MOLECULE_PROFILE_FRESH,
    CascadeConfig,
    CascadeRateController,
    ControlDecisionLogger,
    DataQualityEventLogger,
    QmbGuardConfig,
    QmbSignalGuard,
    StableBandTracker,
    StableConditionTracker,
    TemperaturePidConfig,
    TemperaturePidController,
    exact_calibration_ratio,
    robust_linear_slope,
    robust_median,
    robust_rate_from_thickness,
)

RUN_AUTOMATION_OVERRIDES = load_phase_overrides("heat")

# Monitoring-only pyrometer: displayed/logged, never used for control or safety decisions.
PYROMETER_SETTINGS = load_pyrometer_settings()
PYROMETER_PROFILE = PyrometerProfile(**PYROMETER_SETTINGS)
PYROMETER_SERIAL_CONFIG = PyrometerSerialConfig(port="COM10", baudrate=38400, address="00")


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



init()
stop_event = threading.Event()
data_lock = threading.Lock()
keysight_lock = threading.Lock()
state_lock = threading.Lock()


# Desktop alarm: native topmost Windows popup + repeating critical sound.
# This software warning supplements, but never replaces, hardware interlocks.
PRESSURE_DESKTOP_ALARM_MBAR = 5.0e-6
pressure_emergency_alarm = PressureEmergencyAlarm(
    threshold_mbar=PRESSURE_DESKTOP_ALARM_MBAR,
    context='Phase 01 - Heat up + Calibration',
)
atexit.register(pressure_emergency_alarm.close)
# _____________________AUTOMATION PARAMETERS____________________________________
AUTO_KEYSIGHT_ENABLED = True

# The requested first ramp setpoint remains 0.005 A, but the instrument is
# first enabled and verified at exactly 0 A. One retry is allowed before
# startup fails safely, keeping the sequence deterministic and simple.
KEYSIGHT_START_CURRENT_A = 0.005
KEYSIGHT_STARTUP_ZERO_CURRENT_A = 0.0
KEYSIGHT_STARTUP_ENABLE_ATTEMPTS = 2
KEYSIGHT_STARTUP_VERIFY_DELAY_S = 0.50
KEYSIGHT_OUTPUT_OFF_CONFIRM_DELAY_S = 0.20
KEYSIGHT_BASE_WORK_CURRENT_A = 0.640

# Normal operation cap: the automation must never command above this value.
KEYSIGHT_SOFT_WARNING_A = 0.660

# Software hard current safety value. The script stops if measured current reaches this.
KEYSIGHT_HARD_STOP_A = 0.680

# Hardware latch protection in the Keysight. Keep this above the software hard stop
# to avoid nuisance OCP trips from short transients/readback tolerances while the
# script is intentionally operating near the 0.660 A soft cap.
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
PID_KD_A_PER_C_PER_S = 0.0
PID_MAX_STEP_A = 0.0025
PID_INTEGRAL_LIMIT_C_S = 250.0

# Selectable CK-1 evaporation feedback strategy.
# Temperature mode preserves the established controller. Rate mode uses the
# CK-1 QMB as the primary feedback after a conservative warm-up. Compound mode
# adds a gradual temperature-ceiling override to rate feedback and is the
# recommended mode after supervised hardware validation.
EVAPORATION_CONTROL_MODE = CONTROL_MODE_TEMPERATURE
RATE_CONTROL_MAX_TEMP_C = 250.0
RATE_PID_MIN_CONTROL_TEMP_C = 150.0
RATE_PID_ACTIVATION_A_PER_S = 0.05
RATE_PID_FILTER_POINTS = 11
RATE_PID_CONTROL_PERIOD_S = 60.0
RATE_PID_DEADBAND_A_PER_S = 0.03
RATE_PID_KP_A_PER_RATE = 0.020
RATE_PID_KI_A_PER_THICKNESS = 0.00020
RATE_PID_KD_A_PER_RATE_SLOPE = 0.0
RATE_PID_MAX_UP_STEP_A = 0.0010
RATE_PID_MAX_DOWN_STEP_A = 0.0015
RATE_PID_INTEGRAL_LIMIT_THICKNESS_A = 25.0
RATE_PID_SIGNAL_TIMEOUT_S = 30.0
COMPOUND_TEMP_GUARD_BAND_C = 5.0

# Professional signal estimation and true cascade control.
MOLECULE_CONDITION_PROFILE = 'normal'
FRESH_PROFILE_INITIAL_TARGET_OFFSET_C = -4.0
FRESH_PROFILE_CASCADE_KI_SCALE = 0.0
CONTROL_TEMPERATURE_FILTER_POINTS = 5
RATE_ESTIMATOR_WINDOW_S = 60.0
RATE_ESTIMATOR_MIN_POINTS = 30
RATE_ESTIMATOR_MIN_SPAN_S = 45.0
RATE_ESTIMATOR_MIN_R2 = 0.80
QMB_MAX_ABS_RATE_A_PER_S = 10.0
QMB_MAX_DERIVED_THICKNESS_RATE_A_PER_S = 10.0
QMB_MIN_ALLOWED_THICKNESS_JUMP_A = 5.0
RATE_CONTROL_SETTLING_S = 180.0
CASCADE_INNER_READY_TEMP_BAND_C = 0.75
CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN = 0.30
CASCADE_INNER_READY_STABLE_DURATION_S = 60.0
CASCADE_THERMAL_RESPONSE_MAX_HOLD_S = 420.0
CASCADE_RATE_KP_C_PER_RATE = 1.5
CASCADE_RATE_KI_C_PER_THICKNESS = 0.0025
CASCADE_MAX_TARGET_UP_C_PER_MIN = 0.40
CASCADE_MAX_TARGET_DOWN_C_PER_MIN = 0.40
CASCADE_MAX_TARGET_STEP_C = 1.0
FRESH_PROFILE_MAX_TARGET_STEP_C = 0.75
CASCADE_TARGET_TRIM_LIMIT_C = 8.0
CASCADE_INTEGRAL_LIMIT_THICKNESS_A = 20.0
CASCADE_TREND_HOLD_THRESHOLD_A_PER_S2 = 0.0005
RATE_ESTIMATE_TREND_WINDOW_S = 180.0
RATE_ESTIMATE_TREND_MIN_POINTS = 3
FAST_RATE_EXCURSION_FACTOR = 1.75
FAST_RATE_EXCURSION_DURATION_S = 20.0
FAST_RATE_EXCURSION_CURRENT_STEP_A = 0.0010

# Professional temperature PID details. Safety continues to use raw readings.
PID_INTEGRAL_ACTIVE_ERROR_C = 5.0
PID_DERIVATIVE_FILTER_TAU_S = 20.0
PID_MAX_UP_SLEW_A_PER_MIN = 0.01875
PID_MAX_DOWN_SLEW_A_PER_MIN = 0.01875

# Independent temperature watchdog.
# This is intentionally separate from the PID: if the PID misbehaves, stops
# correcting, or the CK-1 sensor goes stale/unphysical, this layer can still
# force a current reduction or shut the Keysight output off.
TEMP_WATCHDOG_ENABLED = True
TEMP_WATCHDOG_PERIOD_S = 5.0
TEMP_WATCHDOG_SOFT_MARGIN_C = 5.0       # target + this value => force current down
TEMP_WATCHDOG_MAX_TEMP_C = 255.0        # absolute CK-1 temperature => output OFF / SAFETY_STOP
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
TEMP_SLOPE_WINDOW_S = 45.0
TEMP_SLOPE_MIN_SPAN_S = 20.0
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
# Calibration quality is based on synchronized QMB linearity and exact target crossing.
CALIBRATION_MIN_LINEAR_R2 = 0.985
RAMPDOWN_STEP_A = 0.010
RAMPDOWN_STEP_PERIOD_S = 15
RAMPDOWN_ZERO_THRESHOLD_A = 0.003

# Apply validated launcher values only inside this child process. The packaged
# defaults and source files are not modified. Fixed equipment hard stops remain
# outside the editor; the approved run-level watchdog maximum and current cap are editable.
apply_overrides_to_namespace("heat", globals(), RUN_AUTOMATION_OVERRIDES)
KEYSIGHT_INSTRUMENT_OCP_A = KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A
KEYSIGHT_INSTRUMENT_OVP_V = KEYSIGHT_HARD_STOP_V + KEYSIGHT_INSTRUMENT_OVP_MARGIN_V
CK1_RATE_LOW_A_PER_S = CK1_RATE_TARGET_A_PER_S - 0.05
CK1_RATE_HIGH_A_PER_S = CK1_RATE_TARGET_A_PER_S + 0.05

def fresh_molecule_profile_active():
    return str(MOLECULE_CONDITION_PROFILE).strip().lower() == MOLECULE_PROFILE_FRESH


def effective_rate_settling_s():
    # The post-refill dataset showed about 156 s from current-slope changes to
    # temperature-slope response and about 177 s to rate response.  Never let
    # the outer loop act again before that delayed response can be observed.
    base = max(float(RATE_CONTROL_SETTLING_S), 180.0)
    return max(base, 240.0) if fresh_molecule_profile_active() else base


def effective_cascade_inner_ready_stable_s():
    base = max(0.0, float(CASCADE_INNER_READY_STABLE_DURATION_S))
    return max(base, 120.0) if fresh_molecule_profile_active() else base


def effective_cascade_thermal_hold_max_s():
    base = max(60.0, float(CASCADE_THERMAL_RESPONSE_MAX_HOLD_S))
    return max(base, 600.0) if fresh_molecule_profile_active() else base


def effective_rate_deadband():
    return max(float(RATE_PID_DEADBAND_A_PER_S), 0.04) if fresh_molecule_profile_active() else float(RATE_PID_DEADBAND_A_PER_S)


def effective_cascade_up_slew():
    return float(CASCADE_MAX_TARGET_UP_C_PER_MIN) * (0.50 if fresh_molecule_profile_active() else 1.0)


def effective_cascade_down_slew():
    return float(CASCADE_MAX_TARGET_DOWN_C_PER_MIN) * (0.50 if fresh_molecule_profile_active() else 1.0)


def effective_cascade_step_c():
    if fresh_molecule_profile_active():
        return min(float(CASCADE_MAX_TARGET_STEP_C), float(FRESH_PROFILE_MAX_TARGET_STEP_C))
    return float(CASCADE_MAX_TARGET_STEP_C)


def effective_cascade_inner_temp_slope_limit_c_per_min():
    """Return the controller-internal thermal-stability limit for cascade handover.

    This threshold belongs only to the compound controller's inner-loop
    qualification.  It is deliberately independent from the experimental
    shutter-opening criteria.
    """
    base = float(CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN)
    return min(base, 0.20) if fresh_molecule_profile_active() else base


def effective_cascade_trim():
    return min(float(CASCADE_TARGET_TRIM_LIMIT_C), 5.0) if fresh_molecule_profile_active() else float(CASCADE_TARGET_TRIM_LIMIT_C)


def effective_cascade_ki():
    scale = float(FRESH_PROFILE_CASCADE_KI_SCALE) if fresh_molecule_profile_active() else 1.0
    return float(CASCADE_RATE_KI_C_PER_THICKNESS) * max(0.0, min(scale, 1.0))


def initial_cascade_target_c(current_temp=None):
    guide = float(HEATING_TRIGGER_TEMP_C)
    live_target_getter = globals().get('get_heating_trigger_temp_c')
    if callable(live_target_getter):
        try:
            guide = float(live_target_getter())
        except Exception:
            pass
    if not fresh_molecule_profile_active():
        return guide
    target = guide + min(0.0, float(FRESH_PROFILE_INITIAL_TARGET_OFFSET_C))
    target = max(0.0, min(target, float(RATE_CONTROL_MAX_TEMP_C)))
    if current_temp is not None:
        # Never command an upward jump above the data-tuned fresh start point.
        # If handover occurs hotter, begin cooling from the lower target.
        target = min(float(current_temp), target)
    return target


rate_pid_controller = RatePidController(
    RatePidConfig(
        kp_a_per_rate=RATE_PID_KP_A_PER_RATE,
        ki_a_per_thickness=RATE_PID_KI_A_PER_THICKNESS,
        kd_a_per_rate_slope=RATE_PID_KD_A_PER_RATE_SLOPE,
        deadband_rate=effective_rate_deadband(),
        max_up_step_a=RATE_PID_MAX_UP_STEP_A * (0.5 if fresh_molecule_profile_active() else 1.0),
        max_down_step_a=RATE_PID_MAX_DOWN_STEP_A * (2.0 / 3.0 if fresh_molecule_profile_active() else 1.0),
        integral_limit_thickness=RATE_PID_INTEGRAL_LIMIT_THICKNESS_A,
        min_current_a=0.0,
        max_current_a=KEYSIGHT_SOFT_WARNING_A,
    )
)

temperature_pid_controller = TemperaturePidController(
    TemperaturePidConfig(
        kp=PID_KP_A_PER_C,
        ki=PID_KI_A_PER_C_S,
        kd=PID_KD_A_PER_C_PER_S,
        deadband_c=PID_TEMP_BAND_C,
        integral_limit_c_s=PID_INTEGRAL_LIMIT_C_S,
        integral_active_error_c=PID_INTEGRAL_ACTIVE_ERROR_C,
        derivative_tau_s=PID_DERIVATIVE_FILTER_TAU_S,
        max_up_slew_a_per_min=PID_MAX_UP_SLEW_A_PER_MIN,
        max_down_slew_a_per_min=PID_MAX_DOWN_SLEW_A_PER_MIN,
        min_current_a=0.0,
        max_current_a=KEYSIGHT_SOFT_WARNING_A,
    )
)

cascade_rate_controller = CascadeRateController(
    CascadeConfig(
        kp_c_per_rate=CASCADE_RATE_KP_C_PER_RATE,
        ki_c_per_thickness=effective_cascade_ki(),
        deadband_rate=effective_rate_deadband(),
        integral_limit_thickness=CASCADE_INTEGRAL_LIMIT_THICKNESS_A,
        max_up_c_per_min=effective_cascade_up_slew(),
        max_down_c_per_min=effective_cascade_down_slew(),
        trim_limit_c=effective_cascade_trim(),
        settling_s=effective_rate_settling_s(),
        trend_hold_threshold_per_s=CASCADE_TREND_HOLD_THRESHOLD_A_PER_S2,
        temperature_guard_band_c=COMPOUND_TEMP_GUARD_BAND_C,
        max_step_c=effective_cascade_step_c(),
    )
)
cascade_rate_controller.reset(base_target_c=HEATING_TRIGGER_TEMP_C, current_target_c=initial_cascade_target_c(), now_s=time.monotonic())
cascade_inner_ready_tracker = StableConditionTracker(effective_cascade_inner_ready_stable_s())

rate_pid_state = {
    'activated': False,
    'activated_at': None,
    'last_filtered_rate_a_per_s': None,
    'last_rate_timestamp': None,
    'last_log_at': 0.0,
    'hard_stop_triggered': False,
    'last_control_action_at': 0.0,
    'last_outer_action_at': 0.0,
    'last_rate_estimate': None,
    'last_rate_estimate_value': None,
    'last_rate_estimate_sample_timestamp': None,
    'last_valid_estimate_at': None,
    'fast_excursion_since': None,
    'last_fast_guard_action_at': 0.0,
    'last_outer_delta_c': 0.0,
    'last_outer_target_c': None,
    'cascade_inner_ready': False,
    'cascade_inner_ready_elapsed_s': 0.0,
    'cascade_thermal_response_pending': False,
    'cascade_outer_freeze_reason': 'Waiting for inner temperature-loop qualification.',
    'cascade_temp_slope_c_per_min': None,
    'cascade_rate_trend_a_per_s2': None,
}
feedback_control_state = {
    'active_controller': 'Warm-up ramp',
    'last_change_at': time.time(),
    'active_temperature_target_c': HEATING_TRIGGER_TEMP_C,
}

control_history_lock = threading.Lock()
control_signal_history = {
    'thickness_times': [],
    'thickness_data': [],
    'rate_times': [],
    'rate_data': [],
    'estimated_rate_times': [],
    'estimated_rate_data': [],
}
control_decision_logger = ControlDecisionLogger(
    os.path.join(final_folder_path, f"{safe_sample_name}_control_decisions.csv")
)
qmb_guard_config = QmbGuardConfig(
    max_abs_rate_a_per_s=QMB_MAX_ABS_RATE_A_PER_S,
    max_thickness_rate_a_per_s=QMB_MAX_DERIVED_THICKNESS_RATE_A_PER_S,
    min_allowed_thickness_jump_a=QMB_MIN_ALLOWED_THICKNESS_JUMP_A,
)
data_quality_event_logger = DataQualityEventLogger(
    os.path.join(final_folder_path, f"{safe_sample_name}_data_quality_events.csv")
)


def record_qmb_rejection(device_name, signal_name, raw_value, decision):
    ts, formatted, dec = log_timestamp()
    message = (
        f"QMB DATA REJECTED | {device_name} {signal_name}: {raw_value!r} | "
        f"{decision.reason}"
    )
    print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.YELLOW}{message}{Style.RESET_ALL}")
    data_quality_event_logger.log(
        timestamp=ts.isoformat(),
        device=device_name,
        signal=signal_name,
        raw_value=raw_value,
        reason=decision.reason,
        previous_value=decision.previous_value,
        elapsed_s=decision.elapsed_s,
        derived_rate_a_per_s=decision.derived_rate_a_per_s,
    )

try:
    parameter_record_path = write_effective_parameters(
        os.path.join(final_folder_path, f"{safe_sample_name}_automation_parameters.json"),
        "heat",
        RUN_AUTOMATION_OVERRIDES,
    )
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
    'startup_in_progress': False,
    'startup_verified': False,
    'startup_attempts': 0,
    'startup_error': None,
    'startup_verified_at': None,

    'last_soft_cap_warning_at': 0.0,
    'last_voltage_limit_guard_at': 0.0,
}

temperature_pid_state = {
    'last_log_at': 0.0,
    'last_target_c': HEATING_TRIGGER_TEMP_C,
    'last_mode': None,
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
    'baseline_ck1_time': None,
    'baseline_sample_time': None,
    'calibration_result': None,
    'calibration_quality_status': 'not evaluated',
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

GUI_REFRESH_INTERVAL_S = 0.50
DATA_SAVE_INTERVAL_S = 5.0
MAX_PLOT_POINTS_PER_SERIES = 400
AUTOSCALE_EVERY_N_REFRESHES = 10
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
qmb_signal_guards = {name: QmbSignalGuard(qmb_guard_config) for name in QMBs}
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

TEMPERATURE_VIEW_STYLES = {
    # The selected live graph, its title and its selector label deliberately use
    # the same accent so the active signal is obvious at a glance.
    'oven': {
        'accent': '#c62828',       # red
        'inactive': '#fde8e8',
        'active': '#f8caca',
        'hover': '#fbdada',
    },
    'pyrometer': {
        'accent': '#1565c0',       # blue
        'inactive': '#e3f0ff',
        'active': '#c5ddfa',
        'hover': '#d7e8fc',
    },
    'sample': {
        'accent': '#d4a000',       # readable yellow / gold on white
        'inactive': '#fff8cf',
        'active': '#ffed8a',
        'hover': '#fff2ad',
    },
}
TEMPERATURE_TITLE_PAD = 4
TEMPERATURE_SELECTOR_LIFT = 0.040

# _____________________PLOTS____________________________________________________
# The fast Qt dashboard does not need a second 3x3 Matplotlib live canvas. Keep a
# tiny off-screen canvas only for legacy helper calls; report/snapshot figures are
# still created at full resolution when they are actually saved.
if USE_QT_PHASE13_DASHBOARD:
    fig = Figure(figsize=(1.0, 1.0))
    FigureCanvas(fig)
    ax_thickness_ck1 = ax_rate_ck1 = ax_pressure_xgs600 = None
    ax_thickness_sample = ax_rate_sample = ax_temperature_oven = None
    ax_current_keysight = ax_voltage_keysight = ax_temperature_ck1 = None
    line_thickness_ck1 = line_rate_ck1 = None
    line_thickness_sample = line_rate_sample = None
    line_pressure_xgs600 = line_temperature_oven = None
    line_current_keysight = line_voltage_keysight = line_temperature_ck1 = None
    phase_title_text = None
    axis_info_texts = {}
else:
    fig, ((ax_thickness_ck1, ax_rate_ck1, ax_pressure_xgs600),
          (ax_thickness_sample, ax_rate_sample, ax_temperature_oven),
          (ax_current_keysight, ax_voltage_keysight, ax_temperature_ck1)) = plt.subplots(3, 3, figsize=(19.2, 11.2))
    fig.patch.set_facecolor('#f4f6f8')
    # A larger vertical gap gives every graph title its own header space and keeps
    # the temperature selector clear of the plots above and below it.
    plt.subplots_adjust(left=0.052, right=0.685, top=0.875, bottom=0.075, hspace=0.68, wspace=0.30)
    plt.ion()

    line_thickness_ck1, = ax_thickness_ck1.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['ck1'])
    line_rate_ck1, = ax_rate_ck1.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['ck1'])
    line_thickness_sample, = ax_thickness_sample.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['sample'])
    line_rate_sample, = ax_rate_sample.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['sample'])
    line_pressure_xgs600, = ax_pressure_xgs600.plot([], [], linewidth=2.0, color=AXIS_ACCENTS['pressure'])
    line_temperature_oven, = ax_temperature_oven.plot([], [], linewidth=2.0, color=TEMPERATURE_VIEW_STYLES['oven']['accent'])
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
        (ax_temperature_oven, 'Oven PID temperature', 'Temperature (ºC)', TEMPERATURE_VIEW_STYLES['oven']['accent']),
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
snapshot_worker_wakeup = threading.Event()
snapshot_worker_stop_event = threading.Event()
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

if USE_QT_PHASE13_DASHBOARD:
    live_target_temp_line = None
    live_target_rate_line = None
    live_target_rate_low_line = None
    live_target_rate_high_line = None
else:
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

# Shared text state used by both the Matplotlib fallback and the Qt dashboard.
# This must exist before telemetry/status callbacks or Abort / safe stop can run.
live_action_status_lock = threading.Lock()
live_action_status_text = ''

# Live PID/rate/compound selection is independent from editable target values.
# One action lock serializes controller decisions against GUI mode handovers.
feedback_mode_lock = threading.Lock()
feedback_mode_action_lock = threading.Lock()
feedback_mode_state = {
    'mode': str(EVAPORATION_CONTROL_MODE).strip().lower(),
    'generation': 0,
    'previous_mode': None,
    'changed_at': time.time(),
    'reason': 'startup configuration',
}

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




def get_evaporation_control_mode():
    with feedback_mode_lock:
        mode = str(feedback_mode_state.get('mode', EVAPORATION_CONTROL_MODE)).strip().lower()
    if mode not in {CONTROL_MODE_TEMPERATURE, CONTROL_MODE_RATE, CONTROL_MODE_COMPOUND}:
        return CONTROL_MODE_TEMPERATURE
    return mode


def get_evaporation_control_mode_snapshot():
    """Return one atomic mode/generation pair for a controller iteration."""
    with feedback_mode_lock:
        mode = str(feedback_mode_state.get('mode', EVAPORATION_CONTROL_MODE)).strip().lower()
        generation = int(feedback_mode_state.get('generation', 0))
    if mode not in {CONTROL_MODE_TEMPERATURE, CONTROL_MODE_RATE, CONTROL_MODE_COMPOUND}:
        mode = CONTROL_MODE_TEMPERATURE
    return mode, generation


def feedback_mode_is_current(expected_mode, expected_generation):
    with feedback_mode_lock:
        return (
            str(feedback_mode_state.get('mode')).strip().lower() == str(expected_mode).strip().lower()
            and int(feedback_mode_state.get('generation', 0)) == int(expected_generation)
        )


def run_feedback_mode_action(expected_mode, expected_generation, callback, *args, **kwargs):
    """Serialize a controller action against live GUI mode changes.

    A mode switch and one PID/rate action can never mutate controller state or
    command the Keysight at the same time.  If the GUI changed mode after the
    automation thread captured its snapshot, the stale action is discarded.
    """
    with feedback_mode_action_lock:
        if not feedback_mode_is_current(expected_mode, expected_generation):
            return None
        return callback(*args, **kwargs)


def set_evaporation_control_mode(mode, reason='operator request'):
    """Switch the live feedback controller without stepping Keysight current.

    The present current is preserved.  Controller memories and cascade state
    trackers are reinitialized for a bumpless handover, and the normal control
    period must elapse before the selected loop may apply its first correction.
    """
    global EVAPORATION_CONTROL_MODE

    normalized = str(mode).strip().lower()
    valid_modes = {CONTROL_MODE_TEMPERATURE, CONTROL_MODE_RATE, CONTROL_MODE_COMPOUND}
    if normalized not in valid_modes:
        raise ValueError(
            f'Unsupported feedback mode {mode!r}; expected temperature, rate or compound.'
        )
    if stop_event.is_set():
        raise RuntimeError('The phase is stopping; feedback mode can no longer be changed.')

    with feedback_mode_action_lock:
        with feedback_mode_lock:
            previous = str(feedback_mode_state.get('mode', EVAPORATION_CONTROL_MODE)).strip().lower()
            if previous == normalized:
                _set_live_action_status(
                    f'Feedback controller already set to {evaporation_control_mode_label(normalized)}.'
                )
                return False
            EVAPORATION_CONTROL_MODE = normalized
            feedback_mode_state['mode'] = normalized
            feedback_mode_state['generation'] = int(feedback_mode_state.get('generation', 0)) + 1
            feedback_mode_state['previous_mode'] = previous
            feedback_mode_state['changed_at'] = time.time()
            feedback_mode_state['reason'] = str(reason)

        current_setpoint = keysight_state.get('set_current_a')
        if current_setpoint is None:
            current_setpoint = latest_value('Keysight power supply', 'current_data')
        current_temp = latest_control_ck1_temperature()

        # Clear incompatible controller state while preserving hardware output.
        reset_rate_pid(f'live mode switch {previous} -> {normalized}')
        if normalized == CONTROL_MODE_COMPOUND:
            handover_target = initial_cascade_target_c(current_temp)
            cascade_rate_controller.reset(
                base_target_c=get_heating_trigger_temp_c(),
                current_target_c=handover_target,
                now_s=time.monotonic(),
            )
            feedback_control_state['active_temperature_target_c'] = handover_target
            rate_pid_state['last_outer_target_c'] = handover_target
            reset_temperature_pid('live compound handover', handover_target)
        else:
            handover_target = get_heating_trigger_temp_c()
            feedback_control_state['active_temperature_target_c'] = handover_target
            reset_temperature_pid('live feedback-mode handover', handover_target)

        keysight_state['last_step_at'] = time.time()
        controller_label = evaporation_control_mode_label(normalized)
        set_active_feedback_controller(f'Mode-switch settling hold -> {controller_label}')
        current_text = '--' if current_setpoint is None else f'{float(current_setpoint):.3f} A'
        message = (
            f'Feedback controller changed from {evaporation_control_mode_label(previous)} '
            f'to {controller_label}. Keysight current held at {current_text}; '
            'controller state reset bumplessly and settling restarted.'
        )
        _set_live_action_status(message)
        log_control_decision(
            mode='feedback_mode_switch',
            active_controller=f'Mode-switch settling hold -> {controller_label}',
            current_before_a=current_setpoint,
            current_after_a=current_setpoint,
            applied_delta=0.0,
            integral_frozen=True,
            settling=True,
            signal_valid=True,
            reason=f'{reason}: {previous} -> {normalized}; current preserved.',
        )
        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.CYAN}{message}{Style.RESET_ALL}"
        )
    return True


def evaporation_control_mode_label(mode=None):
    mode = mode or get_evaporation_control_mode()
    return {
        CONTROL_MODE_TEMPERATURE: 'Temperature PID',
        CONTROL_MODE_RATE: 'Rate PID',
        CONTROL_MODE_COMPOUND: 'Cascade rate → temperature PID',
    }.get(mode, str(mode))


def uses_rate_feedback():
    return get_evaporation_control_mode() in {CONTROL_MODE_RATE, CONTROL_MODE_COMPOUND}


def get_temperature_watchdog_reference_c():
    if uses_rate_feedback():
        return float(RATE_CONTROL_MAX_TEMP_C)
    return get_heating_trigger_temp_c()


def set_active_feedback_controller(label):
    label = str(label)
    if feedback_control_state.get('active_controller') != label:
        feedback_control_state['active_controller'] = label
        feedback_control_state['last_change_at'] = time.time()


def get_active_feedback_controller():
    return feedback_control_state.get('active_controller', '--')


def active_temperature_target_c():
    return float(feedback_control_state.get('active_temperature_target_c', get_heating_trigger_temp_c()))


def log_control_decision(**values):
    """Append one traceable control decision without ever disrupting the run."""
    try:
        estimate = rate_pid_state.get('last_rate_estimate')
        defaults = {
            'timestamp': datetime.now().isoformat(timespec='milliseconds'),
            'monotonic_s': f"{time.monotonic():.6f}",
            'phase': current_phase() if 'process_state' in globals() else 'INIT',
            'mode': get_evaporation_control_mode(),
            'active_controller': get_active_feedback_controller(),
            'molecule_profile': MOLECULE_CONDITION_PROFILE,
            'raw_temperature_c': latest_ck1_temperature() if 'data' in globals() else None,
            'control_temperature_c': latest_control_ck1_temperature() if 'data' in globals() else None,
            'base_temperature_target_c': get_heating_trigger_temp_c(),
            'active_temperature_target_c': active_temperature_target_c(),
            'raw_qmb_rate_a_per_s': fast_filtered_ck1_rate() if 'control_signal_history' in globals() else None,
            'estimated_rate_a_per_s': rate_pid_state.get('last_filtered_rate_a_per_s'),
            'rate_fit_r_squared': getattr(estimate, 'r_squared', None),
            'rate_fit_span_s': getattr(estimate, 'span_s', None),
            'rate_fit_points': getattr(estimate, 'sample_count', None),
            'rate_target_a_per_s': get_ck1_rate_target_a_per_s(),
            'temperature_slope_c_per_min': rate_pid_state.get('cascade_temp_slope_c_per_min'),
            'rate_trend_a_per_s2': rate_pid_state.get('cascade_rate_trend_a_per_s2'),
            'inner_loop_ready': rate_pid_state.get('cascade_inner_ready'),
            'inner_ready_elapsed_s': rate_pid_state.get('cascade_inner_ready_elapsed_s'),
            'thermal_response_pending': rate_pid_state.get('cascade_thermal_response_pending'),
            'last_outer_action_age_s': (
                None
                if not rate_pid_state.get('last_outer_action_at')
                else max(0.0, time.monotonic() - float(rate_pid_state.get('last_outer_action_at')))
            ),
            'outer_freeze_reason': rate_pid_state.get('cascade_outer_freeze_reason'),
        }
        defaults.update(values)
        control_decision_logger.log(**defaults)
    except Exception as exc:
        now = time.time()
        if now - rate_pid_state.get('last_log_error_at', 0.0) > 60.0:
            print(f"Control-decision logging warning: {exc}")
            rate_pid_state['last_log_error_at'] = now

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
    if USE_QT_PHASE13_DASHBOARD:
        return
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
    reset_rate_pid('Live rate target changed')


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
    global live_action_status_text
    formatted = str(message).replace(' | ', '\n')
    with live_action_status_lock:
        live_action_status_text = formatted

    if USE_QT_PHASE13_DASHBOARD:
        return
    if live_target_status_text is not None:
        live_target_status_text.set_text(_wrap_gui_column_text(formatted))
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
    reset_rate_pid('Manual Keysight current control changed')
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


def apply_manual_current_value(requested_current_a: float, source: str = 'GUI manual current control'):
    try:
        requested_current_a = float(requested_current_a)
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

    set_manual_current_enabled(True, source, emit_message=False)

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


def _apply_manual_current_from_widgets(event=None):
    try:
        requested_current_a = float(live_manual_textboxes['manual_current_a'].text.strip().replace(',', '.'))
    except Exception as e:
        message = f'Could not parse manual Keysight current input: {e}'
        print(message)
        _set_live_action_status(message)
        return
    apply_manual_current_value(requested_current_a, 'GUI manual current control')

def _resume_automatic_current_control(event=None):
    set_manual_current_enabled(False, 'GUI Auto current button')


def confirm_shutter_open(source: str = 'GUI button'):
    ck1_baseline = latest_ck1_thickness()
    sample_baseline = latest_sample_thickness()
    ck1_times = data['CK-1 evaporator QMB']['thickness_times']
    sample_times = data['Sample QMB']['thickness_times']
    ck1_baseline_time = ck1_times[-1] if ck1_times else None
    sample_baseline_time = sample_times[-1] if sample_times else None
    with state_lock:
        process_state['baseline_ck1_thickness'] = ck1_baseline
        process_state['baseline_sample_thickness'] = sample_baseline
        process_state['baseline_ck1_time'] = ck1_baseline_time
        process_state['baseline_sample_time'] = sample_baseline_time
        process_state['calibration_result'] = None
        process_state['calibration_quality_status'] = 'not evaluated'
        process_state['final_thickness_ratio'] = None
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
    """Abort Phase 01 immediately and close only this phase process.

    Abort is deliberately different from Finish Phase: it does not run the
    normal controlled ramp-down.  The hardware-safe priority is to command the
    Keysight to 0 A / OUTPUT OFF first, then let the phase process save what it
    can and exit.  The unified launcher remains open.
    """
    message = (
        'Abort requested from GUI. Keysight is being commanded immediately to '
        '0 A / OUTPUT OFF; Phase 01 will then close.'
    )
    print_banner(message)
    _set_live_action_status(message)

    with state_lock:
        process_state['phase'] = 'SAFETY_STOP'
        process_state['transition_reason'] = message
        process_state['gui_auto_close'] = True

    # Safety action first. Do not delay OUTPUT OFF behind snapshots or file I/O.
    emergency_keysight_shutdown('GUI Abort button - immediate phase stop')

    try:
        request_snapshot('gui_abort')
        save_phase_summary('gui_abort')
    except Exception as e:
        print(f"Could not save GUI abort summary during shutdown: {e}")


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
    active = temperature_view_state.get('mode', 'oven')
    for mode, button in temperature_view_buttons.items():
        style = TEMPERATURE_VIEW_STYLES[mode]
        selected = mode == active
        facecolor = style['active'] if selected else style['inactive']
        button.color = facecolor
        button.hovercolor = style['hover']
        button.label.set_color(style['accent'])
        button.label.set_fontweight('bold')
        button.ax.set_facecolor(facecolor)
        for spine in button.ax.spines.values():
            spine.set_color(style['accent'] if selected else '#cbd5e1')
            spine.set_linewidth(2.0 if selected else 0.9)


def set_temperature_view(mode):
    if mode not in {'oven', 'pyrometer', 'sample'}:
        return
    temperature_view_state['mode'] = mode
    if USE_QT_PHASE13_DASHBOARD:
        print(f"Temperature graph view changed to: {_temperature_view_label(mode)}")
        return
    accent = TEMPERATURE_VIEW_STYLES[mode]['accent']
    line_temperature_oven.set_color(accent)
    ax_temperature_oven.set_title(
        _temperature_view_label(mode),
        fontsize=10.4,
        fontweight='bold',
        color=accent,
        pad=TEMPERATURE_TITLE_PAD,
    )
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

    # Reserve a slim header inside the temperature subplot cell, then lift the
    # selector above the graph title.  The three controls remain visually tied
    # to this plot without covering its dynamic title.
    original_bbox = ax_temperature_oven.get_position()
    selector_height = 0.020
    selector_gap = 0.004
    selector_lift = TEMPERATURE_SELECTOR_LIFT
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
    y = bbox.y1 + selector_lift
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

    # Use the complete right side of the figure.  The extra width keeps labels
    # readable, while the near-full-height panel gives every heading and button
    # its own vertical space on the normal chamber monitor.
    panel_left = 0.705
    panel_bottom = 0.012
    panel_width = 0.282
    panel_height = 0.976

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
    add_panel_card(control_panel_ax, 0.025, 0.390, 0.950, 0.295, facecolor='#f5f1ff', edgecolor='#ddd6fe')
    add_panel_card(control_panel_ax, 0.025, 0.280, 0.950, 0.100, facecolor='#fff7e8', edgecolor='#fde68a')
    add_panel_card(control_panel_ax, 0.025, 0.155, 0.950, 0.120, facecolor='#eefbf3', edgecolor='#bbf7d0')
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
            # Do not bind TextBox.on_submit here. Matplotlib submits a TextBox
            # when it loses focus, which previously sent the current merely by
            # clicking elsewhere in the GUI. Only the explicit button below
            # is allowed to issue a manual Keysight current command.
            live_manual_textboxes[key] = textbox
        else:
            raise ValueError(f'Unknown textbox target: {target}')
        return textbox

    panel_text(0.05, 0.985, 'Heat up + Calibration', fontsize=12.2, color='#0f172a', weight='bold')
    panel_text(0.05, 0.957, 'Live controls and run status', fontsize=8.1, color='#475569')

    # Editable heating targets
    panel_text(0.05, 0.920, 'Editable heating targets', fontsize=9.3, color='#334155', weight='bold')
    target_box_specs = [
        ('trigger_temp_c', 'Temp target / guide (ºC)', f"{HEATING_TRIGGER_TEMP_C:.1f}", 0.850),
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
    panel_text(0.05, 0.382, 'Manual current override', fontsize=9.1, color='#334155', weight='bold')
    add_textbox('manual_current_a', 'Manual I (A)', f"{KEYSIGHT_START_CURRENT_A:.3f}", 0.322, target='manual')

    btn_manual_set = add_button(0.06, 0.282, 0.40, 0.034, 'Set manual current', '#fee2e2', '#fecaca', '#991b1b', fontsize=6.8)
    btn_manual_set.on_clicked(_apply_manual_current_from_widgets)
    live_manual_buttons['set_manual_current'] = btn_manual_set

    btn_auto_current = add_button(0.52, 0.282, 0.40, 0.034, 'Resume automatic', '#dcfce7', '#bbf7d0', '#166534', fontsize=6.8)
    btn_auto_current.on_clicked(_resume_automatic_current_control)
    live_manual_buttons['resume_auto_current'] = btn_auto_current

    # Operator actions
    panel_text(0.05, 0.270, 'Operator controls', fontsize=9.6, color='#334155', weight='bold')
    btn_open = add_button(0.06, 0.212, 0.40, 0.036, 'Open shutter', '#dcfce7', '#bbf7d0', '#166534', fontsize=6.6)
    btn_open.on_clicked(_gui_open_shutter)
    live_target_buttons['open_shutter'] = btn_open

    btn_close = add_button(0.52, 0.212, 0.40, 0.036, 'Close shutter', '#ffedd5', '#fed7aa', '#9a3412', fontsize=6.6)
    btn_close.on_clicked(_gui_close_shutter)
    live_target_buttons['close_shutter'] = btn_close

    btn_abort = add_button(0.06, 0.166, 0.40, 0.036, 'Abort / safe stop', '#fee2e2', '#fecaca', '#991b1b', fontsize=7.8)
    btn_abort.on_clicked(request_gui_abort)
    live_target_buttons['abort'] = btn_abort

    btn_finish = add_button(0.52, 0.166, 0.40, 0.036, 'Finish phase', '#e0f2fe', '#bae6fd', '#075985', fontsize=7.8)
    btn_finish.on_clicked(request_gui_finish)
    live_target_buttons['finish'] = btn_finish

    # Run status and last action
    panel_text(0.05, 0.145, 'Process status', fontsize=9.3, color='#334155', weight='bold')
    live_dashboard_text = control_panel_ax.text(
        0.05, 0.120, '', transform=control_panel_ax.transAxes,
        fontsize=6.35, color='#0f172a', va='top', ha='left', linespacing=0.88,
        clip_on=True
    )

    panel_text(0.55, 0.145, 'Last action', fontsize=9.3, color='#334155', weight='bold')
    live_target_status_text = control_panel_ax.text(
        0.55, 0.120, '', transform=control_panel_ax.transAxes,
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


def copy_plot_snapshot(max_points=MAX_PLOT_POINTS_PER_SERIES):
    """Return a bounded, full-run plot snapshot without copying all history.

    The old GUI copied every recorded point several times per second, then
    discarded most of them for plotting. As runs grew, that increasingly
    blocked mouse events. This samples each series while retaining its first-to-
    last time span and always includes the newest value.
    """

    def sample(values):
        n = len(values)
        if n <= max_points:
            return list(values)
        step = max(1, math.ceil(n / max_points))
        sampled = list(values[::step])
        if sampled and sampled[-1] != values[-1]:
            sampled.append(values[-1])
        return sampled

    with data_lock:
        return {
            device: {series_key: sample(series_values) for series_key, series_values in series.items()}
            for device, series in data.items()
        }


def periodic_data_saver():
    """Write the existing complete text files outside the GUI event loop."""

    while not stop_event.wait(DATA_SAVE_INTERVAL_S):
        try:
            write_data_files(copy_data_snapshot())
        except Exception as exc:
            print(f'Periodic data save failed: {exc}')

    # Preserve one final complete save during normal finish or safe stop.
    try:
        write_data_files(copy_data_snapshot())
    except Exception as exc:
        print(f'Final data save failed: {exc}')


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
        f'Feedback mode: {evaporation_control_mode_label()}',
        f'Molecule profile: {MOLECULE_CONDITION_PROFILE}',
        f'Active control: {get_active_feedback_controller()}',
        f'Active T target: {active_temperature_target_c():.2f} ºC',
        f'Shutter: {shutter_status}',
        f'Current mode: Manual {manual_mode}',
        f'Manual I: {float(manual_applied):.3f} A' if manual_applied is not None else 'Manual I: --',
        f'Ramp: {ramp_mode}',
        f'Steps until: {ramp_settings["steps_until_temp_c"]:.1f} ºC',
        f'Step period: {ramp_settings["steps_step_period_s"]:.1f} s',
        f'Slopes E/M/L: {ramp_settings["slope_early_c_per_min"]:.1f}/'
        f'{ramp_settings["slope_mid_c_per_min"]:.1f}/'
        f'{ramp_settings["slope_late_c_per_min"]:.1f}',
        f'Target T / guide: {targets["trigger_temp_c"]:.1f} ºC',
        f'Rate-mode max T: {RATE_CONTROL_MAX_TEMP_C:.1f} ºC',
        f'Target rate: {targets["rate_target_a_per_s"]:.3f} Å/s',
        f'Band: {targets["rate_low_a_per_s"]:.3f}-{targets["rate_high_a_per_s"]:.3f}',
        f'CK-1 T: {current_temp:.1f} ºC' if current_temp is not None else 'CK-1 T: --',
        f'CK-1 rate raw: {current_rate:.3f} Å/s' if current_rate is not None else 'CK-1 rate raw: --',
        f'Rate estimate: {filtered_ck1_rate():.3f} Å/s' if filtered_ck1_rate() is not None else 'Rate estimate: --',
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


def latest_control_ck1_temperature():
    """Median-filtered temperature for control only; watchdog stays raw."""
    with data_lock:
        values = list(data['Arduino CK-1 crucible temperature']['temperature_data'])
    filtered = robust_median(values, CONTROL_TEMPERATURE_FILTER_POINTS)
    return latest_ck1_temperature() if filtered is None else filtered


def estimate_ck1_rate_from_thickness():
    """Return one cached robust estimate per new CK-1 thickness sample."""
    with control_history_lock:
        times = list(control_signal_history['thickness_times'])
        thickness = list(control_signal_history['thickness_data'])
    sample_timestamp = times[-1] if times else None
    if sample_timestamp == rate_pid_state.get('last_rate_estimate_sample_timestamp'):
        return rate_pid_state.get('last_rate_estimate_value'), rate_pid_state.get('last_rate_estimate')

    estimate = robust_rate_from_thickness(
        times, thickness,
        window_s=RATE_ESTIMATOR_WINDOW_S,
        min_points=RATE_ESTIMATOR_MIN_POINTS,
        min_span_s=RATE_ESTIMATOR_MIN_SPAN_S,
    )
    rate_pid_state['last_rate_estimate_sample_timestamp'] = sample_timestamp
    rate_pid_state['last_rate_estimate'] = estimate
    value = None
    if estimate.valid and estimate.value_per_s is not None:
        if estimate.r_squared is not None and estimate.r_squared >= RATE_ESTIMATOR_MIN_R2:
            value = max(0.0, float(estimate.value_per_s))
            rate_pid_state['last_valid_estimate_at'] = time.monotonic()
            with control_history_lock:
                control_signal_history['estimated_rate_times'].append(sample_timestamp)
                control_signal_history['estimated_rate_data'].append(value)
                # Keep a bounded history while retaining several trend windows.
                keep_after = sample_timestamp.timestamp() - max(900.0, 4.0 * float(RATE_ESTIMATE_TREND_WINDOW_S))
                while (
                    len(control_signal_history['estimated_rate_times']) > 2
                    and control_signal_history['estimated_rate_times'][0].timestamp() < keep_after
                ):
                    control_signal_history['estimated_rate_times'].pop(0)
                    control_signal_history['estimated_rate_data'].pop(0)
    rate_pid_state['last_rate_estimate_value'] = value
    return value, estimate


def filtered_ck1_rate(num_points=RATE_PID_FILTER_POINTS):
    value, _estimate = estimate_ck1_rate_from_thickness()
    return value


def fast_filtered_ck1_rate(num_points=RATE_PID_FILTER_POINTS):
    with control_history_lock:
        values = list(control_signal_history['rate_data'])
    value = robust_rate_average(values, num_points)
    return None if value is None else max(0.0, float(value))


def latest_ck1_rate_timestamp():
    with control_history_lock:
        values = control_signal_history['thickness_times']
        return values[-1] if values else None


def latest_ck1_rate_age_s():
    timestamp = latest_ck1_rate_timestamp()
    if timestamp is None:
        return None
    try:
        return max(0.0, time.time() - timestamp.timestamp())
    except Exception:
        return None


def estimate_ck1_rate_trend_per_s():
    # Trend decisions use the same robust thickness-derived rate as the outer
    # loop, never the noisy instantaneous QMB rate.  The window is comparable
    # to the measured current-to-rate delay, so the controller can recognize
    # a response already moving in the correct direction.
    with control_history_lock:
        times = list(control_signal_history['estimated_rate_times'])
        values = list(control_signal_history['estimated_rate_data'])
    estimate = robust_linear_slope(
        times, values,
        window_s=RATE_ESTIMATE_TREND_WINDOW_S,
        min_points=max(3, int(RATE_ESTIMATE_TREND_MIN_POINTS)),
        min_span_s=min(120.0, max(60.0, float(RATE_ESTIMATE_TREND_WINDOW_S) * 0.5)),
    )
    if not estimate.valid or estimate.value_per_s is None:
        return None
    return float(estimate.value_per_s)


def estimate_ck1_temp_slope_c_per_min(num_points=TEMP_SLOPE_WINDOW_POINTS):
    with data_lock:
        times = list(data['Arduino CK-1 crucible temperature']['temperature_times'])
        values = list(data['Arduino CK-1 crucible temperature']['temperature_data'])
    estimate = robust_linear_slope(
        times, values,
        window_s=TEMP_SLOPE_WINDOW_S,
        min_points=max(2, int(num_points)),
        min_span_s=TEMP_SLOPE_MIN_SPAN_S,
    )
    if not estimate.valid or estimate.value_per_s is None:
        return None
    return float(estimate.value_per_s) * 60.0


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
        set_active_feedback_controller('Slope ramp waiting for valid temperature trend')
        log_control_decision(
            mode='slope_ramp', active_controller='Slope ramp hold',
            current_before_a=current_setpoint, current_after_a=current_setpoint,
            signal_valid=False, integral_frozen=True,
            reason='Insufficient robust temperature-slope history; current held.',
        )
        return False, None, None, 0.0

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
        log_control_decision(
            mode='slope_ramp', active_controller='Slope ramp hold', error=slope_error,
            current_before_a=current_setpoint, current_after_a=current_setpoint,
            inside_deadband=True, signal_valid=True,
            reason=f'Robust temperature slope {temp_slope:.4f} ºC/min in deadband.',
        )
        return False, temp_slope, target_slope, 0.0

    raw_delta = TEMP_SLOPE_KP_A_PER_C_PER_MIN * slope_error
    max_step = max_temp_control_step_a(current_setpoint)
    delta_a = clamp(raw_delta, -max_step, +max_step)
    before = float(current_setpoint)
    changed = nudge_keysight_current(
        delta_a,
        f'Temp-slope control: measured={temp_slope:.3f} ºC/min, target={target_slope:.3f} ºC/min, error={slope_error:.3f} ºC/min',
        max_current=KEYSIGHT_SOFT_WARNING_A,
    )
    after = keysight_state.get('set_current_a')
    log_control_decision(
        mode='slope_ramp', active_controller='Slope ramp', error=slope_error,
        p_term=raw_delta, requested_delta=delta_a,
        applied_delta=(float(after) - before if after is not None else 0.0),
        current_before_a=before, current_after_a=after,
        slew_or_step_limited=abs(raw_delta - delta_a) > 1e-12,
        signal_valid=True,
        reason=f'Robust temperature slope {temp_slope:.4f} ºC/min; target {target_slope:.4f}.',
    )
    return changed, temp_slope, target_slope, delta_a


def clamp(value, low, high):
    return max(low, min(value, high))


def heating_ready_for_shutter(ck1_temp, ck1_rate_avg):
    """Return True when the operator-facing Phase 01 process targets are met.

    Shutter progression is intentionally simple and auditable: CK-1 temperature
    must be at or above its target/guide and the averaged CK-1 QMB rate must be
    at or above its target.  Controller bands, rate trends, temperature slopes,
    settling timers and current headroom do not gate this state transition.
    """
    target_temp = get_heating_trigger_temp_c()
    target_rate = get_ck1_rate_target_a_per_s()
    return (
        ck1_temp is not None
        and ck1_rate_avg is not None
        and float(ck1_temp) >= float(target_temp)
        and float(ck1_rate_avg) >= float(target_rate)
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
    snapshot_worker_wakeup.set()


def _pop_pending_snapshot_tags():
    with pending_snapshots_lock:
        if not pending_snapshots:
            return []
        tags = pending_snapshots[:]
        pending_snapshots.clear()
        return tags


def process_pending_snapshots():
    # Compatibility helper used by shutdown paths. Rendering is deliberately
    # performed by snapshot_saver_worker, never by the Matplotlib mouse/event loop.
    snapshot_worker_wakeup.set()


def snapshot_saver_worker():
    """Render graph snapshots off the GUI thread so clicks stay responsive."""

    while True:
        snapshot_worker_wakeup.wait(0.50)
        snapshot_worker_wakeup.clear()

        while True:
            tags = _pop_pending_snapshot_tags()
            if not tags:
                break
            for tag in tags:
                save_snapshot(tag)

        if snapshot_worker_stop_event.is_set():
            with pending_snapshots_lock:
                queue_empty = not pending_snapshots
            if queue_empty:
                break


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
        keysight_state['startup_in_progress'] = False
        keysight_state['startup_verified'] = False
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
        color=TEMPERATURE_VIEW_STYLES['oven']['accent'],
        label='Oven PID',
    )
    fax_temperature_oven.plot(
        source['IMPAC pyrometer']['temperature_times'],
        source['IMPAC pyrometer']['temperature_data'],
        linewidth=1.6,
        color=TEMPERATURE_VIEW_STYLES['pyrometer']['accent'],
        label='Pyrometer raw',
    )
    fax_temperature_oven.plot(
        source['IMPAC pyrometer']['temperature_times'],
        source['IMPAC pyrometer']['sample_temperature_data'],
        linewidth=1.6,
        color=TEMPERATURE_VIEW_STYLES['sample']['accent'],
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


def calculate_calibration_result():
    """Return the shared exact-crossing calibration result as a summary dict."""
    result = exact_calibration_ratio(
        sample_times=data['Sample QMB']['thickness_times'],
        sample_thickness=data['Sample QMB']['thickness_data'],
        ck1_times=data['CK-1 evaporator QMB']['thickness_times'],
        ck1_thickness=data['CK-1 evaporator QMB']['thickness_data'],
        sample_baseline_a=process_state.get('baseline_sample_thickness'),
        ck1_baseline_a=process_state.get('baseline_ck1_thickness'),
        sample_start_time=process_state.get('baseline_sample_time'),
        ck1_start_time=process_state.get('baseline_ck1_time'),
        target_sample_a=CALIBRATION_TARGET_SAMPLE_A,
        minimum_linearity_r2=CALIBRATION_MIN_LINEAR_R2,
    )
    return result.as_dict()


def calculate_thickness_ratio():
    """Return the frozen exact-crossing ratio, or a live ratio before completion."""
    result = process_state.get('calibration_result') or {}
    frozen = result.get('ratio')
    if frozen is not None:
        return frozen
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
            f.write(f"evaporation_feedback_mode: {get_evaporation_control_mode()}\n")
            f.write(f"molecule_condition_profile: {MOLECULE_CONDITION_PROFILE}\n")
            f.write(f"active_feedback_controller: {get_active_feedback_controller()}\n")
            f.write(f"active_temperature_target_c: {active_temperature_target_c()}\n")
            f.write(f"filtered_ck1_rate_a_per_s: {filtered_ck1_rate()}\n")
            estimate = rate_pid_state.get('last_rate_estimate')
            f.write(f"rate_fit_r_squared: {getattr(estimate, 'r_squared', None)}\n")
            f.write(f"rate_fit_span_s: {getattr(estimate, 'span_s', None)}\n")
            f.write(f"control_decision_log: {control_decision_logger.path}\n")
            f.write(f"data_quality_event_log: {data_quality_event_logger.path}\n")
            f.write(f"rate_control_temperature_ceiling_c: {RATE_CONTROL_MAX_TEMP_C}\n")
            f.write(f"watchdog_maximum_temperature_c: {TEMP_WATCHDOG_MAX_TEMP_C}\n")
            f.write(f"maximum_automatic_current_cap_a: {KEYSIGHT_SOFT_WARNING_A}\n")
            f.write(f"cascade_inner_ready: {rate_pid_state.get('cascade_inner_ready')}\n")
            f.write(f"cascade_inner_ready_elapsed_s: {rate_pid_state.get('cascade_inner_ready_elapsed_s')}\n")
            f.write(f"cascade_thermal_response_pending: {rate_pid_state.get('cascade_thermal_response_pending')}\n")
            f.write(f"cascade_outer_freeze_reason: {rate_pid_state.get('cascade_outer_freeze_reason')}\n")
            f.write(f"cascade_temperature_slope_c_per_min: {rate_pid_state.get('cascade_temp_slope_c_per_min')}\n")
            f.write(f"cascade_rate_trend_a_per_s2: {rate_pid_state.get('cascade_rate_trend_a_per_s2')}\n")
            f.write(f"cascade_inner_ready_temp_band_c: {CASCADE_INNER_READY_TEMP_BAND_C}\n")
            f.write(f"cascade_inner_ready_stable_duration_s: {effective_cascade_inner_ready_stable_s()}\n")
            f.write(f"cascade_thermal_response_max_hold_s: {effective_cascade_thermal_hold_max_s()}\n")
            f.write(f"ck1_temp_c: {latest_ck1_temperature()}\n")
            f.write(f"ck1_thickness_a: {latest_ck1_thickness()}\n")
            f.write(f"sample_thickness_a: {latest_sample_thickness()}\n")
            f.write(f"keysight_set_current_a: {keysight_state['set_current_a']}\n")
            f.write(f"keysight_hold_current_a: {keysight_state['hold_current_a']}\n")
            f.write(f"sample_relative_thickness_a: {relative_sample_thickness()}\n")
            f.write(f"ck1_relative_thickness_a: {relative_ck1_thickness()}\n")
            f.write(f"thickness_ratio: {calculate_thickness_ratio()}\n")
            calibration_result = process_state.get('calibration_result') or {}
            f.write(f"calibration_quality_status: {process_state.get('calibration_quality_status')}\n")
            for field in (
                'crossing_timestamp', 'sample_target_a', 'ck1_relative_at_crossing_a',
                'fit_slope_ratio', 'linearity_r2', 'quality_pass', 'quality_message',
                'synchronized_fit_points',
            ):
                f.write(f"calibration_{field}: {calibration_result.get(field)}\n")
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
    """Enable the Keysight safely at 0 A, then apply the first ramp current.

    The startup is completed synchronously before telemetry/control threads start.
    The instrument is given one normal enable attempt and one retry, both at 0 A.
    A failed startup leaves the supply at 0 A with output OFF.
    """
    keysight_state['startup_in_progress'] = True
    keysight_state['startup_verified'] = False
    keysight_state['startup_attempts'] = 0
    keysight_state['startup_error'] = None
    keysight_state['automation_active'] = False

    try:
        keysight_write('SYST:REM')
        keysight_write(f'VOLT:RANG {KEYSIGHT_RANGE}')
        keysight_write('*CLS')

        # Hardware latches remain above the software hard stops so short
        # readback transients do not create nuisance trips near the normal cap.
        keysight_write(f'VOLT:PROT {KEYSIGHT_INSTRUMENT_OVP_V:.3f}')
        keysight_write('VOLT:PROT:STAT ON')
        keysight_write(f'CURR:PROT {KEYSIGHT_INSTRUMENT_OCP_A:.3f}')
        keysight_write('CURR:PROT:STAT ON')
        keysight_set_voltage_limit(KEYSIGHT_VOLTAGE_LIMIT_V)

        # Start from one deterministic, electrically benign state. The first
        # non-zero current is never sent until OUTP ON has been confirmed at 0 A.
        keysight_set_current(KEYSIGHT_STARTUP_ZERO_CURRENT_A)
        keysight_write('OUTP OFF')

        verified = False
        last_status = None
        for attempt in range(1, KEYSIGHT_STARTUP_ENABLE_ATTEMPTS + 1):
            keysight_state['startup_attempts'] = attempt
            keysight_write('VOLT:PROT:CLE')
            keysight_write('CURR:PROT:CLE')
            keysight_write('OUTP ON')
            time.sleep(KEYSIGHT_STARTUP_VERIFY_DELAY_S)
            last_status = keysight_protection_status()
            if (
                last_status.get('output_on') is True
                and last_status.get('ocp_tripped') is not True
                and last_status.get('ovp_tripped') is not True
            ):
                verified = True
                break

            # Only one retry is permitted. Keep the retry at 0 A and return to
            # a known output-OFF state before trying to enable again.
            keysight_set_current(KEYSIGHT_STARTUP_ZERO_CURRENT_A)
            keysight_write('OUTP OFF')

        if not verified:
            raise RuntimeError(
                'Keysight startup verification failed after '
                f'{KEYSIGHT_STARTUP_ENABLE_ATTEMPTS} attempts (one retry); '
                f'last status={last_status!r}'
            )

        requested_start_current = clamp(
            float(KEYSIGHT_START_CURRENT_A),
            KEYSIGHT_STARTUP_ZERO_CURRENT_A,
            normal_current_cap_a(),
        )
        keysight_set_current(requested_start_current)

        now = time.time()
        keysight_state['startup_verified'] = True
        keysight_state['startup_verified_at'] = now
        keysight_state['automation_active'] = True
        keysight_state['last_step_at'] = now
        keysight_state['automation_started_at'] = now
        keysight_state['reason_stopped'] = None
        print_banner(
            'KEYSIGHT STARTUP VERIFIED\n'
            'Output was enabled and confirmed at 0.000 A before applying the '
            f'{requested_start_current:.3f} A ramp start setpoint.\n'
            f'OUTP verification attempts: {keysight_state["startup_attempts"]}.'
        )

    except Exception as exc:
        keysight_state['startup_error'] = str(exc)
        force_keysight_zero_output(f'Keysight startup failed: {exc}')
        raise
    finally:
        keysight_state['startup_in_progress'] = False

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
    if USE_QT_PHASE13_DASHBOARD:
        return
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

    desired_temperature_title = _temperature_view_label(temperature_mode)
    temperature_accent = TEMPERATURE_VIEW_STYLES[temperature_mode]['accent']
    if line_temperature_oven.get_color() != temperature_accent:
        line_temperature_oven.set_color(temperature_accent)
    if (
        ax_temperature_oven.get_title() != desired_temperature_title
        or ax_temperature_oven.title.get_color() != temperature_accent
    ):
        ax_temperature_oven.set_title(
            desired_temperature_title,
            fontsize=10.6,
            fontweight='bold',
            color=temperature_accent,
            pad=TEMPERATURE_TITLE_PAD,
        )
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
                                decision = qmb_signal_guards[key].check_thickness(thickness_value, ts)
                                if decision.accepted:
                                    with data_lock:
                                        data[key]['thickness_times'].append(ts)
                                        data[key]['thickness_data'].append(thickness_value)
                                    if key == 'CK-1 evaporator QMB':
                                        with control_history_lock:
                                            control_signal_history['thickness_times'].append(ts)
                                            control_signal_history['thickness_data'].append(thickness_value)
                                    print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.GREEN}{key} Thickness: {thickness_value} Å{Style.RESET_ALL}")
                                else:
                                    record_qmb_rejection(key, 'thickness', thickness_value, decision)
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
                                decision = qmb_signal_guards[key].check_rate(rate_value)
                                if decision.accepted:
                                    with data_lock:
                                        data[key]['rate_times'].append(ts)
                                        data[key]['rate_data'].append(rate_value)
                                    if key == 'CK-1 evaporator QMB':
                                        with control_history_lock:
                                            control_signal_history['rate_times'].append(ts)
                                            control_signal_history['rate_data'].append(rate_value)
                                    print(f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - {Fore.GREEN}{key} Rate: {rate_value} Å/s{Style.RESET_ALL}")
                                else:
                                    record_qmb_rejection(key, 'rate', rate_value, decision)
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
            pressure_emergency_alarm.update(pressure_value)
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
                keysight_state.get('startup_verified', False)
                and not keysight_state.get('startup_in_progress', False)
                and output_on is False
                and expected_current > RAMPDOWN_ZERO_THRESHOLD_A
                and current_phase() not in ('RAMP_DOWN', 'FINISHED', 'SAFETY_STOP')
            )

            # A single stale/transitioning OUTP? response must not abort a run.
            # Confirm once more before classifying an unexpected output-OFF.
            if unexpected_output_off:
                time.sleep(KEYSIGHT_OUTPUT_OFF_CONFIRM_DELAY_S)
                confirmation = keysight_protection_status()
                ocp_tripped = bool(ocp_tripped or confirmation.get('ocp_tripped') is True)
                ovp_tripped = bool(ovp_tripped or confirmation.get('ovp_tripped') is True)
                unexpected_output_off = confirmation.get('output_on') is False

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






def reset_rate_pid(reason: str = ''):
    rate_pid_controller.reset()
    reset_target = initial_cascade_target_c()
    cascade_rate_controller.reset(
        base_target_c=get_heating_trigger_temp_c(),
        current_target_c=reset_target,
        now_s=time.monotonic(),
    )
    cascade_inner_ready_tracker.reset()
    rate_pid_state['activated'] = False
    rate_pid_state['activated_at'] = None
    rate_pid_state['last_filtered_rate_a_per_s'] = None
    rate_pid_state['last_rate_timestamp'] = None
    rate_pid_state['last_valid_estimate_at'] = None
    rate_pid_state['last_rate_estimate'] = None
    rate_pid_state['last_rate_estimate_value'] = None
    rate_pid_state['last_rate_estimate_sample_timestamp'] = None
    rate_pid_state['hard_stop_triggered'] = False
    rate_pid_state['last_control_action_at'] = 0.0
    rate_pid_state['last_outer_action_at'] = 0.0
    rate_pid_state['last_outer_eval_at'] = 0.0
    rate_pid_state['fast_excursion_since'] = None
    rate_pid_state['last_fast_guard_action_at'] = 0.0
    rate_pid_state['last_outer_delta_c'] = 0.0
    rate_pid_state['last_outer_target_c'] = reset_target
    rate_pid_state['cascade_inner_ready'] = False
    rate_pid_state['cascade_inner_ready_elapsed_s'] = 0.0
    rate_pid_state['cascade_thermal_response_pending'] = False
    rate_pid_state['cascade_outer_freeze_reason'] = 'Waiting for inner temperature-loop qualification.'
    rate_pid_state['cascade_temp_slope_c_per_min'] = None
    rate_pid_state['cascade_rate_trend_a_per_s2'] = None
    feedback_control_state['active_temperature_target_c'] = reset_target
    if reason:
        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.CYAN}Rate/cascade control reset: {reason}{Style.RESET_ALL}"
        )


def _rate_feedback_hard_stop(reason: str):
    if rate_pid_state.get('hard_stop_triggered'):
        return
    rate_pid_state['hard_stop_triggered'] = True
    rate_pid_controller.reset()
    reset_temperature_pid('Rate-feedback safety stop')
    set_phase('SAFETY_STOP', reason)
    request_snapshot('rate_feedback_hard_stop')
    save_phase_summary('rate_feedback_hard_stop')
    stop_keysight_output(reason)
    stop_event.set()
    print_banner(
        "RATE FEEDBACK HARD STOP\n"
        f"{reason}\n"
        "Keysight current was forced to 0 A and output was switched OFF."
    )


def rate_feedback_can_control(current_temp, filtered_rate):
    if not uses_rate_feedback():
        return False
    if current_temp is None or float(current_temp) < RATE_PID_MIN_CONTROL_TEMP_C:
        return False
    if filtered_rate is None:
        return False
    rate_age_s = latest_ck1_rate_age_s()
    if rate_age_s is None or rate_age_s > RATE_PID_SIGNAL_TIMEOUT_S:
        return False

    if not rate_pid_state.get('activated'):
        activation_threshold = min(
            RATE_PID_ACTIVATION_A_PER_S,
            max(0.0, get_ck1_rate_target_a_per_s() * 0.5),
        )
        if float(filtered_rate) < activation_threshold:
            return False
        rate_pid_controller.reset()
        handover_target = initial_cascade_target_c(current_temp)
        cascade_rate_controller.reset(
            base_target_c=get_heating_trigger_temp_c(),
            current_target_c=handover_target,
            now_s=time.monotonic(),
        )
        cascade_inner_ready_tracker.reset()
        rate_pid_state['last_outer_delta_c'] = 0.0
        rate_pid_state['last_outer_action_at'] = 0.0
        rate_pid_state['last_outer_target_c'] = handover_target
        rate_pid_state['cascade_inner_ready'] = False
        rate_pid_state['cascade_inner_ready_elapsed_s'] = 0.0
        rate_pid_state['cascade_thermal_response_pending'] = False
        rate_pid_state['cascade_outer_freeze_reason'] = 'Handover: waiting for inner temperature-loop qualification.'
        feedback_control_state['active_temperature_target_c'] = handover_target
        reset_temperature_pid('rate feedback handover', handover_target)
        rate_pid_state['activated'] = True
        rate_pid_state['activated_at'] = time.time()
        print_banner(
            f"Rate feedback activated at T={float(current_temp):.2f} ºC and "
            f"filtered CK-1 rate={float(filtered_rate):.3f} Å/s.\n"
            f"Selected mode: {evaporation_control_mode_label()}. "
            f"Initial cascade temperature target={handover_target:.2f} ºC; "
            f"outer settling={effective_rate_settling_s():.0f} s."
        )
    return True


def check_active_rate_signal_or_stop():
    if not uses_rate_feedback() or not rate_pid_state.get('activated'):
        return True
    age_s = latest_ck1_rate_age_s()
    if age_s is None or age_s > RATE_PID_SIGNAL_TIMEOUT_S:
        _rate_feedback_hard_stop(
            'CK-1 thickness feedback became unavailable or stale after control handover '
            f'(age={age_s if age_s is not None else "unknown"} s; '
            f'limit={RATE_PID_SIGNAL_TIMEOUT_S:.1f} s).'
        )
        return False

    last_valid = rate_pid_state.get('last_valid_estimate_at')
    invalid_for_s = None if last_valid is None else max(0.0, time.monotonic() - float(last_valid))
    if invalid_for_s is None or invalid_for_s > RATE_PID_SIGNAL_TIMEOUT_S:
        estimate = rate_pid_state.get('last_rate_estimate')
        reason = getattr(estimate, 'reason', 'quality criterion not met') if estimate is not None else 'no estimate'
        _rate_feedback_hard_stop(
            'CK-1 robust thickness-slope estimate remained quality-invalid after control handover '
            f'(invalid for {invalid_for_s if invalid_for_s is not None else "unknown"} s; '
            f'limit={RATE_PID_SIGNAL_TIMEOUT_S:.1f} s; reason={reason}).'
        )
        return False
    return True


def apply_rate_pid_control(current_temp, current_setpoint=None, filtered_rate=None):
    """Conservative direct-current rate mode retained for diagnostics."""
    if filtered_rate is None:
        filtered_rate = filtered_ck1_rate()
    if filtered_rate is None:
        return False
    if current_setpoint is None:
        current_setpoint = keysight_state.get('set_current_a')
    if current_setpoint is None:
        current_setpoint = latest_value('Keysight power supply', 'current_data') or 0.0

    now_mono = time.monotonic()
    if now_mono - rate_pid_state.get('last_control_action_at', 0.0) < effective_rate_settling_s():
        set_active_feedback_controller('Direct rate PI settling hold')
        log_control_decision(
            mode='rate_direct', active_controller='Direct rate PI settling hold',
            estimated_rate_a_per_s=filtered_rate,
            current_before_a=current_setpoint, current_after_a=current_setpoint,
            settling=True, integral_frozen=True, signal_valid=True,
            reason='Waiting for thermal response to previous direct-rate action.',
        )
        return False

    decision = rate_pid_controller.update(
        target_rate=get_ck1_rate_target_a_per_s(),
        measured_rate=filtered_rate,
        current_setpoint_a=current_setpoint,
        now_s=now_mono,
        compound_temperature_guard=True,
        current_temperature_c=current_temp,
        maximum_temperature_c=RATE_CONTROL_MAX_TEMP_C,
        temperature_guard_band_c=0.0,
    )
    rate_pid_state['last_filtered_rate_a_per_s'] = float(filtered_rate)
    rate_pid_state['last_rate_timestamp'] = latest_ck1_rate_timestamp()
    now = time.time()
    keysight_state['last_step_at'] = now
    set_active_feedback_controller('Direct rate PI diagnostic')

    before = float(current_setpoint)
    changed = False
    if abs(decision.delta_a) >= 1e-9:
        changed = nudge_keysight_current(
            decision.delta_a,
            (
                f'Direct rate PI: robust thickness-slope rate={filtered_rate:.3f} Å/s, '
                f'target={get_ck1_rate_target_a_per_s():.3f} Å/s, '
                f'error={decision.error_rate:+.3f} Å/s, requested={decision.raw_delta_a:+.5f} A'
            ),
            max_current=KEYSIGHT_SOFT_WARNING_A,
        )
    after = keysight_state.get('set_current_a')
    applied = float(after) - before if after is not None else 0.0
    if changed:
        rate_pid_state['last_control_action_at'] = now_mono
    log_control_decision(
        mode='rate_direct', active_controller='Direct rate PI diagnostic',
        estimated_rate_a_per_s=filtered_rate, error=decision.error_rate,
        requested_delta=decision.raw_delta_a, applied_delta=applied,
        current_before_a=before, current_after_a=after,
        inside_deadband=decision.inside_deadband,
        temperature_limited=decision.temperature_limited,
        signal_valid=True,
        reason='Direct rate mode uses robust thickness-slope feedback.',
    )
    return changed



def cascade_inner_loop_snapshot(current_temp, target_temp, now_mono, rate_error):
    """Qualify the inner thermal loop before the outer rate PI can stack actions.

    Historical manual pre-refill runs show that CK-1 rate may continue moving
    for several minutes after current or temperature changes.  The outer loop
    therefore waits for the temperature PID to reach and stabilize at its
    present target before requesting another same-direction increase.  High-rate
    downward corrections remain available, but repeated downward actions are
    held while the previous cooling response is still propagating.
    """
    temp_value = None if current_temp is None else float(current_temp)
    target_value = float(target_temp)
    temp_slope = estimate_ck1_temp_slope_c_per_min()
    band_c = max(0.1, float(CASCADE_INNER_READY_TEMP_BAND_C))
    slope_limit = max(0.01, effective_cascade_inner_temp_slope_limit_c_per_min())

    condition = (
        temp_value is not None
        and temp_slope is not None
        and abs(temp_value - target_value) <= band_c
        and abs(float(temp_slope)) <= slope_limit
    )
    inner_ready = cascade_inner_ready_tracker.update(condition, now_mono)
    inner_elapsed = cascade_inner_ready_tracker.elapsed(now_mono)

    last_delta = float(rate_pid_state.get('last_outer_delta_c') or 0.0)
    last_action_at = float(rate_pid_state.get('last_outer_action_at') or 0.0)
    action_age = None if last_action_at <= 0.0 else max(0.0, now_mono - last_action_at)
    pending = False
    if (
        action_age is not None
        and action_age < effective_cascade_thermal_hold_max_s()
        and abs(last_delta) > 1e-12
        and temp_value is not None
    ):
        propagation_slope = max(0.05, slope_limit * 0.5)
        if last_delta > 0.0:
            pending = (
                temp_value < target_value - band_c
                or (temp_slope is not None and float(temp_slope) > propagation_slope)
            )
        else:
            pending = (
                temp_value > target_value + band_c
                or (temp_slope is not None and float(temp_slope) < -propagation_slope)
            )

    deadband = effective_rate_deadband()
    freeze_reason = ''
    freeze_outer = False
    # A low measured rate during warm-up is not a steady-state rate error.
    # Never ratchet the temperature guide upward until the inner loop has
    # demonstrated equilibrium around the current target.
    if float(rate_error) > deadband and not inner_ready:
        freeze_outer = True
        freeze_reason = (
            f'Inner temperature loop not qualified: |T-target| must be <= {band_c:.2f} ºC '
            f'and |dT/dt| <= {slope_limit:.2f} ºC/min for '
            f'{effective_cascade_inner_ready_stable_s():.0f} s.'
        )

    # Do not repeat a correction in the same direction while its thermal
    # response is visibly still propagating. Opposite-direction recovery is
    # deliberately not blocked.
    if pending and last_delta * float(rate_error) > 0.0:
        freeze_outer = True
        freeze_reason = (
            f'Previous {last_delta:+.3f} ºC target action is still propagating '
            f'(age={action_age:.0f} s, dT/dt={temp_slope if temp_slope is not None else float("nan"):+.3f} ºC/min).'
        )

    rate_pid_state['cascade_inner_ready'] = bool(inner_ready)
    rate_pid_state['cascade_inner_ready_elapsed_s'] = float(inner_elapsed)
    rate_pid_state['cascade_thermal_response_pending'] = bool(pending)
    rate_pid_state['cascade_outer_freeze_reason'] = freeze_reason
    rate_pid_state['cascade_temp_slope_c_per_min'] = temp_slope
    return {
        'inner_ready': bool(inner_ready),
        'inner_elapsed_s': float(inner_elapsed),
        'temperature_slope_c_per_min': temp_slope,
        'thermal_response_pending': bool(pending),
        'last_action_age_s': action_age,
        'freeze_outer': bool(freeze_outer),
        'freeze_reason': freeze_reason,
    }


def apply_compound_cascade_control(current_temp, current_setpoint=None, filtered_rate=None):
    """True cascade: rate PI adjusts T target; temperature PID adjusts current."""
    if filtered_rate is None:
        filtered_rate = filtered_ck1_rate()
    if filtered_rate is None or current_temp is None:
        set_active_feedback_controller('Cascade waiting for valid signals')
        log_control_decision(
            mode='compound_cascade', active_controller='Cascade signal hold',
            current_before_a=current_setpoint, current_after_a=current_setpoint,
            signal_valid=False, integral_frozen=True,
            reason='Robust thickness-slope rate or filtered temperature unavailable.',
        )
        return False
    if current_setpoint is None:
        current_setpoint = keysight_state.get('set_current_a')
    if current_setpoint is None:
        current_setpoint = latest_value('Keysight power supply', 'current_data') or 0.0

    now_mono = time.monotonic()
    target_before = float(cascade_rate_controller.target)
    rate_error = get_ck1_rate_target_a_per_s() - float(filtered_rate)
    inner = cascade_inner_loop_snapshot(
        current_temp,
        target_before,
        now_mono,
        rate_error,
    )

    if now_mono - rate_pid_state.get('last_outer_eval_at', 0.0) >= RATE_PID_CONTROL_PERIOD_S:
        trend = estimate_ck1_rate_trend_per_s()
        rate_pid_state['cascade_rate_trend_a_per_s2'] = trend
        outer = cascade_rate_controller.update(
            target_rate=get_ck1_rate_target_a_per_s(),
            measured_rate=filtered_rate,
            rate_trend_per_s=trend,
            base_target_c=get_heating_trigger_temp_c(),
            max_temp_c=RATE_CONTROL_MAX_TEMP_C,
            now_s=now_mono,
            deadband_rate=effective_rate_deadband(),
            freeze=inner['freeze_outer'],
        )
        rate_pid_state['last_outer_eval_at'] = now_mono
        rate_pid_state['last_filtered_rate_a_per_s'] = float(filtered_rate)
        rate_pid_state['last_rate_timestamp'] = latest_ck1_rate_timestamp()
        feedback_control_state['active_temperature_target_c'] = outer.target_c

        if abs(outer.delta_c) > 1e-9:
            rate_pid_state['last_control_action_at'] = now_mono
            rate_pid_state['last_outer_action_at'] = now_mono
            rate_pid_state['last_outer_delta_c'] = float(outer.delta_c)
            rate_pid_state['last_outer_target_c'] = float(outer.target_c)
            cascade_inner_ready_tracker.reset()
            rate_pid_state['cascade_inner_ready'] = False
            rate_pid_state['cascade_inner_ready_elapsed_s'] = 0.0
            # Derivative is calculated on measurement, so a small slew-limited
            # setpoint change does not require resetting the inner PID. Keeping
            # its state avoids repeated loss of integral memory every outer step.

        if inner['freeze_outer']:
            decision_reason = inner['freeze_reason']
        elif outer.settling:
            decision_reason = 'Minimum post-action settling interval remains active.'
        elif outer.trend_hold:
            decision_reason = 'Robust rate is already moving toward target; no further target action.'
        else:
            decision_reason = 'Outer rate PI evaluated the CK-1 temperature target.'

        log_control_decision(
            mode='compound_outer', active_controller='Cascade outer rate PI',
            estimated_rate_a_per_s=filtered_rate,
            active_temperature_target_c=outer.target_c,
            error=outer.error_rate, p_term=outer.p_c, i_term=outer.i_c,
            requested_delta=outer.raw_delta_c, applied_delta=outer.delta_c,
            inside_deadband=outer.inside_deadband, integral_frozen=outer.integral_frozen,
            current_or_target_limited=outer.limited,
            settling=outer.settling, trend_hold=outer.trend_hold,
            signal_valid=True,
            temperature_slope_c_per_min=inner['temperature_slope_c_per_min'],
            rate_trend_a_per_s2=trend,
            inner_loop_ready=inner['inner_ready'],
            inner_ready_elapsed_s=inner['inner_elapsed_s'],
            thermal_response_pending=inner['thermal_response_pending'],
            last_outer_action_age_s=inner['last_action_age_s'],
            outer_freeze_reason=inner['freeze_reason'],
            reason=decision_reason,
        )

    target_temp = float(cascade_rate_controller.target)
    feedback_control_state['active_temperature_target_c'] = target_temp
    set_active_feedback_controller('Cascade rate PI → temperature PID')
    return apply_temperature_pid_control(current_temp, current_setpoint, target_temp_override=target_temp)


def apply_fast_rate_excursion_guard(current_setpoint=None):
    """Separate exceptional guard for a sustained instantaneous high rate."""
    fast_rate = fast_filtered_ck1_rate()
    if fast_rate is None:
        rate_pid_state['fast_excursion_since'] = None
        return False
    threshold = get_ck1_rate_target_a_per_s() * FAST_RATE_EXCURSION_FACTOR
    now_mono = time.monotonic()
    if fast_rate <= threshold:
        rate_pid_state['fast_excursion_since'] = None
        return False
    if rate_pid_state.get('fast_excursion_since') is None:
        rate_pid_state['fast_excursion_since'] = now_mono
        return False
    if now_mono - rate_pid_state['fast_excursion_since'] < FAST_RATE_EXCURSION_DURATION_S:
        return False
    if now_mono - rate_pid_state.get('last_fast_guard_action_at', 0.0) < effective_rate_settling_s():
        return False
    if current_setpoint is None:
        current_setpoint = keysight_state.get('set_current_a') or 0.0
    before = float(current_setpoint)
    changed = nudge_keysight_current(
        -abs(FAST_RATE_EXCURSION_CURRENT_STEP_A),
        f'Fast rate excursion guard: sustained {fast_rate:.3f} Å/s > {threshold:.3f} Å/s',
        max_current=KEYSIGHT_SOFT_WARNING_A,
    )
    after = keysight_state.get('set_current_a')
    rate_pid_state['last_fast_guard_action_at'] = now_mono
    rate_pid_state['last_control_action_at'] = now_mono
    rate_pid_state['fast_excursion_since'] = now_mono
    reset_temperature_pid('fast-rate guard current reduction', active_temperature_target_c())
    log_control_decision(
        mode='fast_rate_guard', active_controller='Fast rate excursion guard',
        raw_qmb_rate_a_per_s=fast_rate,
        current_before_a=before, current_after_a=after,
        requested_delta=-abs(FAST_RATE_EXCURSION_CURRENT_STEP_A),
        applied_delta=(float(after) - before if after is not None else 0.0),
        settling=True, signal_valid=True,
        reason=f'Sustained fast rate exceeded {threshold:.3f} Å/s.',
    )
    return changed


def reset_temperature_pid(reason: str = '', target_temp_override=None):
    target = get_heating_trigger_temp_c() if target_temp_override is None else float(target_temp_override)
    measurement = latest_control_ck1_temperature()
    temperature_pid_controller.reset(
        now_s=time.monotonic(),
        measurement_c=measurement,
        target_c=target,
        bumpless=True,
    )
    temperature_pid_state['last_target_c'] = target
    temperature_pid_state['last_mode'] = get_evaporation_control_mode()
    if reason:
        ts, formatted, dec = log_timestamp()
        print(
            f"{formatted}.{Fore.LIGHTBLACK_EX}{dec}{Style.RESET_ALL} - "
            f"{Fore.CYAN}Temperature PID reset bumplessly: {reason}{Style.RESET_ALL}"
        )


def apply_temperature_pid_control(current_temp, current_setpoint=None, target_temp_override=None):
    """Regulate Keysight current from median-filtered CK-1 temperature."""
    if current_temp is None:
        return False
    target_temp = get_heating_trigger_temp_c() if target_temp_override is None else float(target_temp_override)
    feedback_control_state['active_temperature_target_c'] = target_temp
    if current_setpoint is None:
        current_setpoint = keysight_state.get('set_current_a')
    if current_setpoint is None:
        current_setpoint = latest_value('Keysight power supply', 'current_data') or 0.0

    last_target = temperature_pid_state.get('last_target_c')
    last_mode = temperature_pid_state.get('last_mode')
    if last_target is None or last_mode != get_evaporation_control_mode():
        reset_temperature_pid('controller handover', target_temp)
        return False
    if abs(float(last_target) - target_temp) > 0.05:
        # Retarget without resetting: derivative-on-measurement prevents kick,
        # and the outer loop already limits target movement to a slow slew.
        temperature_pid_state['last_target_c'] = target_temp

    decision = temperature_pid_controller.update(
        target_c=target_temp,
        measurement_c=float(current_temp),
        current_a=float(current_setpoint),
        now_s=time.monotonic(),
        deadband_c=get_pid_temp_band_c(),
    )
    temperature_pid_state['last_target_c'] = target_temp
    temperature_pid_state['last_mode'] = get_evaporation_control_mode()
    # Mark every evaluation, including dead-band holds, so integration and
    # derivative updates never run at GUI-loop speed.
    keysight_state['last_step_at'] = time.time()

    if decision.inside_deadband or abs(decision.delta_a) < 1e-9:
        now = time.time()
        if now - temperature_pid_state.get('last_log_at', 0.0) >= 15.0:
            print(
                f"Temperature PID hold: filtered T={float(current_temp):.2f} ºC, "
                f"target={target_temp:.2f} ºC, band=±{get_pid_temp_band_c():.2f} ºC; "
                f"current={float(current_setpoint):.3f} A."
            )
            temperature_pid_state['last_log_at'] = now
        log_control_decision(
            mode='temperature_inner', active_controller='Temperature PID hold',
            control_temperature_c=current_temp, active_temperature_target_c=target_temp,
            error=decision.error_c, p_term=decision.p_a, i_term=decision.i_a, d_term=decision.d_a,
            requested_delta=decision.raw_delta_a, applied_delta=0.0,
            current_before_a=current_setpoint, current_after_a=current_setpoint,
            inside_deadband=decision.inside_deadband, integral_frozen=decision.integral_frozen,
            slew_or_step_limited=decision.slew_limited,
            current_or_target_limited=decision.current_limited,
            signal_valid=True, reason='Temperature PID hold/no effective correction.',
        )
        return False

    before = float(current_setpoint)
    changed = nudge_keysight_current(
        decision.delta_a,
        (
            f'Temperature PID professional: filtered T={float(current_temp):.2f} ºC, '
            f'target={target_temp:.2f} ºC, error={decision.error_c:+.2f} ºC, '
            f'P={decision.p_a:+.5f}, I={decision.i_a:+.5f}, D={decision.d_a:+.5f} A'
        ),
        max_current=KEYSIGHT_SOFT_WARNING_A,
    )
    after = keysight_state.get('set_current_a')
    if changed:
        keysight_state['last_step_at'] = time.time()
    log_control_decision(
        mode='temperature_inner', active_controller='Temperature PID',
        control_temperature_c=current_temp, active_temperature_target_c=target_temp,
        error=decision.error_c, p_term=decision.p_a, i_term=decision.i_a, d_term=decision.d_a,
        requested_delta=decision.raw_delta_a,
        applied_delta=(float(after) - before if after is not None else 0.0),
        current_before_a=before, current_after_a=after,
        inside_deadband=decision.inside_deadband, integral_frozen=decision.integral_frozen,
        slew_or_step_limited=decision.slew_limited,
        current_or_target_limited=decision.current_limited,
        signal_valid=True, reason='Professional temperature PID decision.',
    )
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
    rate_pid_controller.reset()
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
    rate_pid_controller.reset()
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
            f"Absolute watchdog hard stop remains at {TEMP_WATCHDOG_MAX_TEMP_C:.1f} ºC."
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
            target_temp = get_temperature_watchdog_reference_c()
            soft_limit = target_temp + TEMP_WATCHDOG_SOFT_MARGIN_C
            hard_limit = float(TEMP_WATCHDOG_MAX_TEMP_C)
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
                    f'watchdog maximum={TEMP_WATCHDOG_MAX_TEMP_C:.2f} ºC; reference={target_temp:.2f} ºC).'
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
        if not keysight_state.get('startup_verified'):
            raise RuntimeError('Keysight automation started before zero-current output verification')
        initial_ramp = get_live_ramp_settings()
        print_banner(
            f"Keysight automation started with feedback mode: {evaporation_control_mode_label()}.\n"
            f"Warm-up uses {ramp_mode_label(initial_ramp['mode'])}. Rate/compound handover requires "
            f"T >= {RATE_PID_MIN_CONTROL_TEMP_C:.1f} ºC and a valid robust thickness-slope rate.\n"
            f"Molecule profile: {MOLECULE_CONDITION_PROFILE}. "
            f"Rate estimator: {RATE_ESTIMATOR_WINDOW_S:.0f} s window, "
            f"minimum {RATE_ESTIMATOR_MIN_POINTS} points / {RATE_ESTIMATOR_MIN_SPAN_S:.0f} s.\n"
            f"Compound architecture: rate PI -> temperature target -> temperature PID -> current.\n"
            f"Temperature guide: {get_heating_trigger_temp_c():.1f} ºC; "
            f"rate-control ceiling: {RATE_CONTROL_MAX_TEMP_C:.1f} ºC; "
            f"watchdog maximum: {TEMP_WATCHDOG_MAX_TEMP_C:.1f} ºC.\n"
            f"Current cap {KEYSIGHT_SOFT_WARNING_A:.3f} A; software hard stop "
            f"{KEYSIGHT_HARD_STOP_A:.3f} A; Keysight OCP {KEYSIGHT_INSTRUMENT_OCP_A:.3f} A."
        )

        active_phases = ('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'CALIBRATION', 'WAIT_SHUTTER_CLOSE')
        while not stop_event.is_set():
            phase = current_phase()
            if phase not in active_phases:
                set_active_feedback_controller('Inactive')
                time.sleep(0.5)
                continue
            if not keysight_state['automation_active']:
                time.sleep(0.5)
                continue

            raw_temp = latest_ck1_temperature()
            current_temp = latest_control_ck1_temperature()
            ck1_rate_avg = average_ck1_rate()
            filtered_rate = filtered_ck1_rate()
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
                now_epoch = time.time()
                if now_epoch - keysight_state.get('last_soft_cap_warning_at', 0.0) >= 15.0:
                    print(
                        f"Soft current cap active: holding at {current_setpoint:.3f} A. "
                        f"Hard stop remains {KEYSIGHT_HARD_STOP_A:.3f} A."
                    )
                    keysight_state['last_soft_cap_warning_at'] = now_epoch

            if manual_current_is_enabled():
                set_active_feedback_controller('Manual current')
                reset_temperature_pid('manual-current handover', active_temperature_target_c())
                time.sleep(0.5)
                continue

            mode, mode_generation = get_evaporation_control_mode_snapshot()
            if mode in {CONTROL_MODE_RATE, CONTROL_MODE_COMPOUND} and rate_pid_state.get('activated'):
                if run_feedback_mode_action(
                    mode,
                    mode_generation,
                    apply_fast_rate_excursion_guard,
                    current_setpoint,
                ):
                    keysight_state['last_step_at'] = time.time()
                    time.sleep(0.5)
                    continue

            now_epoch = time.time()
            control_period_s = RATE_PID_CONTROL_PERIOD_S if mode == CONTROL_MODE_RATE else PID_CONTROL_PERIOD_S
            if now_epoch - keysight_state['last_step_at'] < control_period_s:
                time.sleep(0.5)
                continue
            if not feedback_mode_is_current(mode, mode_generation):
                time.sleep(0.1)
                continue

            if mode == CONTROL_MODE_TEMPERATURE:
                set_active_feedback_controller(
                    'Temperature PID' if phase != 'HEATING_UP' or should_use_temperature_pid(current_temp)
                    else 'Warm-up ramp'
                )
                if phase != 'HEATING_UP' or should_use_temperature_pid(current_temp):
                    run_feedback_mode_action(
                        mode,
                        mode_generation,
                        apply_temperature_pid_control,
                        current_temp,
                        current_setpoint,
                    )
                    time.sleep(0.5)
                    continue
                if heating_ready_for_shutter(current_temp, ck1_rate_avg):
                    keysight_state['last_step_at'] = now_epoch
                    time.sleep(0.5)
                    continue
            else:
                signal_ok = run_feedback_mode_action(
                    mode,
                    mode_generation,
                    check_active_rate_signal_or_stop,
                )
                if signal_ok is None:
                    time.sleep(0.1)
                    continue
                if not signal_ok:
                    time.sleep(0.5)
                    continue
                can_control = run_feedback_mode_action(
                    mode,
                    mode_generation,
                    rate_feedback_can_control,
                    current_temp,
                    filtered_rate,
                )
                if can_control is None:
                    time.sleep(0.1)
                    continue
                if can_control:
                    if mode == CONTROL_MODE_COMPOUND:
                        run_feedback_mode_action(
                            mode,
                            mode_generation,
                            apply_compound_cascade_control,
                            current_temp,
                            current_setpoint,
                            filtered_rate,
                        )
                    else:
                        run_feedback_mode_action(
                            mode,
                            mode_generation,
                            apply_rate_pid_control,
                            current_temp,
                            current_setpoint,
                            filtered_rate,
                        )
                    time.sleep(0.5)
                    continue
                if rate_pid_state.get('activated'):
                    if mode == CONTROL_MODE_COMPOUND:
                        # Keep the inner thermal loop stable at the last valid
                        # target while a quality-valid rate estimate is rebuilt.
                        set_active_feedback_controller('Cascade rate signal hold; temperature PID active')
                        run_feedback_mode_action(
                            mode,
                            mode_generation,
                            apply_temperature_pid_control,
                            current_temp,
                            current_setpoint,
                            target_temp_override=active_temperature_target_c(),
                        )
                    else:
                        set_active_feedback_controller('Direct rate signal hold')
                        keysight_state['last_step_at'] = now_epoch
                    time.sleep(0.5)
                    continue
                if phase != 'HEATING_UP':
                    set_active_feedback_controller('Rate handover not established; current hold')
                    keysight_state['last_step_at'] = now_epoch
                    time.sleep(0.5)
                    continue
                if current_temp is not None and float(current_temp) >= RATE_CONTROL_MAX_TEMP_C:
                    set_active_feedback_controller('Temperature ceiling hold before rate handover')
                    run_feedback_mode_action(
                        mode,
                        mode_generation,
                        apply_temperature_pid_control,
                        current_temp,
                        current_setpoint,
                        target_temp_override=RATE_CONTROL_MAX_TEMP_C,
                    )
                    time.sleep(0.5)
                    continue
                set_active_feedback_controller('Warm-up ramp before rate handover')

            maybe_auto_switch_steps_to_slope(current_temp)
            step_period_s = current_ramp_step_period_s(current_temp, current_setpoint)
            if now_epoch - keysight_state['last_step_at'] < step_period_s:
                time.sleep(0.5)
                continue
            if should_use_steps_ramp(current_temp):
                settings = get_live_ramp_settings()
                temp_text = '--' if current_temp is None else f'{float(current_temp):.2f}'
                nudge_keysight_current(
                    +KEYSIGHT_STEP_A,
                    f'Steps ramp: filtered CK-1 T={temp_text} ºC < {settings["steps_until_temp_c"]:.1f} ºC',
                    max_current=KEYSIGHT_SOFT_WARNING_A,
                )
            else:
                apply_temperature_slope_control(current_setpoint)
            keysight_state['last_step_at'] = time.time()
            time.sleep(0.5)

    except Exception as e:
        print(f'Error in Keysight automation: {e}')
        emergency_keysight_shutdown(f'Keysight automation failure: {e}')

def process_controller():
    print_banner(
        "Process phases loaded: HEATING_UP -> WAIT_SHUTTER_OPEN -> CALIBRATION -> WAIT_SHUTTER_CLOSE -> RAMP_DOWN -> FINISHED\n"
        "Use the GUI buttons for Open Shutter, Close Shutter, Abort, and Finish. "
        "Terminal shortcuts still work: 'o' open, 'c' close, 'r' ratio, 'h' targets, 'q' stop."
    )

    while not stop_event.is_set():
        phase = current_phase()
        ck1_temp = latest_control_ck1_temperature()
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
                trigger_reason = (
                    f'Process targets reached: CK-1 T={float(ck1_temp):.2f} ºC >= '
                    f'{get_heating_trigger_temp_c():.2f} ºC and average CK-1 rate='
                    f'{float(ck1_rate_avg):.3f} Å/s >= {get_ck1_rate_target_a_per_s():.3f} Å/s. '
                    f'Feedback mode: {evaporation_control_mode_label()}.'
                )

            if trigger_reason is not None:
                request_snapshot('heating_end')
                save_phase_summary('heating_end')
                with state_lock:
                    process_state['snapshot_taken'] = True
                    process_state['shutter_open_confirmed'] = False
                    process_state['shutter_close_confirmed'] = False
                set_phase('WAIT_SHUTTER_OPEN', trigger_reason)
                print_banner(
                    f"Heating phase finished: {trigger_reason}\n"
                    f"{evaporation_control_mode_label()} remains active; current is not frozen in HOLD.\n"
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
                calibration_result = calculate_calibration_result()
                thickness_ratio = calibration_result.get('ratio')
                process_state['calibration_result'] = calibration_result
                process_state['calibration_quality_status'] = (
                    'PASS' if calibration_result.get('quality_pass') else 'REVIEW / REPEAT RECOMMENDED'
                )
                process_state['final_thickness_ratio'] = thickness_ratio
                request_snapshot('calibration_end')
                save_phase_summary('calibration_end')

                ratio_message = 'Thickness ratio could not be calculated.'
                if thickness_ratio is not None:
                    ratio_message = (
                        f'Exact-crossing thickness ratio (CK-1 / Sample at '
                        f'{float(CALIBRATION_TARGET_SAMPLE_A):.3f} Å) = {thickness_ratio:.3f}\n'
                        f'QMB linearity R² = {calibration_result.get("linearity_r2")}\n'
                        f'{calibration_result.get("quality_message")}'
                    )
                else:
                    ratio_message += f' {calibration_result.get("quality_message")}'

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


# _____________________FAST QT DASHBOARD ADAPTER_______________________________
def qt_set_feedback_mode(mode):
    set_evaporation_control_mode(mode, 'PySide6 GUI live controller selection')


def qt_apply_heating_targets(trigger_temp_c, rate_target_a_per_s, pid_temp_band_c):
    set_live_heating_targets(trigger_temp_c, rate_target_a_per_s, pid_temp_band_c)


def qt_reset_heating_targets():
    defaults = DEFAULT_LIVE_HEATING_TARGETS
    set_live_heating_targets(
        defaults['trigger_temp_c'],
        defaults['rate_target_a_per_s'],
        defaults['pid_temp_band_c'],
    )


def qt_apply_ramp_settings(mode, steps_until_temp_c, steps_step_period_s,
                           slope_early_c_per_min, slope_mid_c_per_min,
                           slope_late_c_per_min):
    set_live_ramp_settings(
        mode=mode,
        steps_until_temp_c=steps_until_temp_c,
        steps_step_period_s=steps_step_period_s,
        slope_early_c_per_min=slope_early_c_per_min,
        slope_mid_c_per_min=slope_mid_c_per_min,
        slope_late_c_per_min=slope_late_c_per_min,
    )


def qt_reset_ramp_settings():
    defaults = DEFAULT_LIVE_RAMP_SETTINGS
    set_live_ramp_settings(
        mode=defaults['mode'],
        steps_until_temp_c=defaults['steps_until_temp_c'],
        steps_step_period_s=defaults['steps_step_period_s'],
        slope_early_c_per_min=defaults['slope_early_c_per_min'],
        slope_mid_c_per_min=defaults['slope_mid_c_per_min'],
        slope_late_c_per_min=defaults['slope_late_c_per_min'],
    )


def qt_set_manual_current(requested_current_a):
    apply_manual_current_value(requested_current_a, 'PySide6 GUI manual current control')


def qt_resume_automatic_current():
    set_manual_current_enabled(False, 'PySide6 GUI Auto current button')


def qt_open_shutter():
    confirm_shutter_open('PySide6 GUI button')


def qt_close_shutter():
    confirm_shutter_closed('PySide6 GUI button')


def _qt_last_action_text():
    with live_action_status_lock:
        return str(live_action_status_text)


def _qt_shutter_status():
    with state_lock:
        shutter_open = process_state.get('shutter_open_confirmed', False)
        shutter_closed = process_state.get('shutter_close_confirmed', False)
    if shutter_open:
        return 'OPEN confirmed'
    if shutter_closed:
        return 'CLOSED confirmed'
    return 'not confirmed'


def _qt_value_line(label, value, fmt, unit=''):
    if value is None:
        return f'{label}: --'
    suffix = f' {unit}' if unit else ''
    return f'{label}: {format(float(value), fmt)}{suffix}'



def qt_dashboard_status():
    targets = get_live_heating_targets()
    ramp = get_live_ramp_settings()
    manual = get_manual_current_state()
    phase = current_phase()
    rel_ck1 = relative_ck1_thickness()
    rel_sample = relative_sample_thickness()
    manual_applied = manual.get('last_applied_current_a')
    lines = [
        f'Phase: {phase}',
        f'Feedback mode: {evaporation_control_mode_label()}',
        f'Molecule profile: {MOLECULE_CONDITION_PROFILE}',
        f'Active control: {get_active_feedback_controller()}',
        f'Active T target: {active_temperature_target_c():.2f} ºC',
        f'Shutter: {_qt_shutter_status()}',
        f'Current mode: {"MANUAL" if manual.get("enabled") else "AUTO"}',
        _qt_value_line('Manual I', manual_applied, '.3f', 'A'),
        f'Ramp: {ramp_mode_label(ramp["mode"])}',
        f'Steps until: {ramp["steps_until_temp_c"]:.1f} ºC',
        f'Step period: {ramp["steps_step_period_s"]:.1f} s',
        f'Slopes E/M/L: {ramp["slope_early_c_per_min"]:.1f}/'
        f'{ramp["slope_mid_c_per_min"]:.1f}/{ramp["slope_late_c_per_min"]:.1f}',
        f'Target T / guide: {targets["trigger_temp_c"]:.1f} ºC',
        f'Rate-mode max T: {RATE_CONTROL_MAX_TEMP_C:.1f} ºC',
        f'Target rate: {targets["rate_target_a_per_s"]:.3f} Å/s',
        f'Control rate band: {targets["rate_low_a_per_s"]:.3f}-{targets["rate_high_a_per_s"]:.3f} Å/s',
        f'Cascade inner ready: {"YES" if rate_pid_state.get("cascade_inner_ready") else "NO"} '
        f'({float(rate_pid_state.get("cascade_inner_ready_elapsed_s") or 0.0):.0f}/'
        f'{effective_cascade_inner_ready_stable_s():.0f} s)',
        f'Thermal response pending: {"YES" if rate_pid_state.get("cascade_thermal_response_pending") else "NO"}',
        f'Outer hold: {rate_pid_state.get("cascade_outer_freeze_reason") or "none"}',
        _qt_value_line('CK-1 relative', rel_ck1, '.2f', 'Å'),
        _qt_value_line('Sample relative', rel_sample, '.2f', 'Å'),
        _qt_value_line('CK-1 T', latest_ck1_temperature(), '.1f', 'ºC'),
        _qt_value_line('CK-1 rate raw', latest_value('CK-1 evaporator QMB', 'rate_data'), '.3f', 'Å/s'),
        _qt_value_line('Rate estimate', filtered_ck1_rate(), '.3f', 'Å/s'),
        _qt_value_line('Current', latest_value('Keysight power supply', 'current_data'), '.3f', 'A'),
        _qt_value_line('Voltage', latest_value('Keysight power supply', 'voltage_data'), '.3f', 'V'),
    ]
    return {
        'phase': phase,
        'phase_label': phase_title_for_display(phase),
        'status_lines': lines,
        'last_action': _qt_last_action_text(),
        'feedback_mode': get_evaporation_control_mode(),
        'feedback_mode_label': evaporation_control_mode_label(),
        'active_feedback_controller': get_active_feedback_controller(),
        'temperature_view': temperature_view_state.get('mode', 'oven'),
        'targets': targets,
        'ramp': ramp,
        'manual': manual,
    }


def build_qt_dashboard_spec():
    from npg_chamber.common.qt_phase_dashboard import PhaseDashboardSpec

    return PhaseDashboardSpec(
        window_title='Phase 01 · Heat up + Calibration',
        phase_name='Heat up + Calibration',
        snapshot_provider=copy_plot_snapshot,
        status_provider=qt_dashboard_status,
        stop_event=stop_event,
        apply_targets=qt_apply_heating_targets,
        reset_targets=qt_reset_heating_targets,
        apply_ramp=qt_apply_ramp_settings,
        reset_ramp=qt_reset_ramp_settings,
        set_manual_current=qt_set_manual_current,
        resume_automatic_current=qt_resume_automatic_current,
        open_shutter=qt_open_shutter,
        close_shutter=qt_close_shutter,
        abort=request_gui_abort,
        finish=request_gui_finish,
        set_temperature_view=set_temperature_view,
        set_feedback_mode=qt_set_feedback_mode,
        refresh_interval_ms=250,
        max_plot_points=MAX_PLOT_POINTS_PER_SERIES,
    )

# _____________________MAIN_______________________________________________________
def main():
    try:
        if keysight_state['automation_enabled']:
            configure_keysight_for_automation()
    except Exception as exc:
        print_banner(
            'KEYSIGHT STARTUP FAILED / PHASE NOT STARTED\n'
            f'{exc}\n'
            'The supply was returned to 0 A with output OFF.'
        )
        emergency_keysight_shutdown(f'Keysight startup failure before threads: {exc}')
        return
    threads = [
        threading.Thread(target=monitor_qmb, daemon=True),
        threading.Thread(target=read_pressure, daemon=True),
        threading.Thread(target=periodic_data_saver, daemon=True),
        threading.Thread(target=snapshot_saver_worker, daemon=True),
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

    try:
        if USE_QT_PHASE13_DASHBOARD:
            from npg_chamber.common.qt_phase_dashboard import run_phase_dashboard

            print('Starting fast PySide6 + PyQtGraph dashboard ...')
            run_phase_dashboard(build_qt_dashboard_spec())
        else:
            print(
                'Fast Qt dashboard unavailable or disabled; using the previous '
                'Matplotlib GUI fallback.'
            )
            setup_live_target_controls()
            setup_temperature_view_selector()
            show_live_plot_window()
            last_plot_refresh_at = 0.0

            while not stop_event.is_set():
                now = time.time()
                if now - last_plot_refresh_at >= GUI_REFRESH_INTERVAL_S:
                    snapshot = copy_plot_snapshot()
                    update_live_plot(snapshot)
                    last_plot_refresh_at = now
                safe_live_plot_refresh(0.005)
    except KeyboardInterrupt:
        print('(▀̿Ĺ̯▀̿ ̿) Stop all the threads!!!')
        emergency_keysight_shutdown('KeyboardInterrupt / manual stop')
    except Exception as e:
        print(f'Fatal error in main loop: {e}')
        emergency_keysight_shutdown(f'Fatal error in main loop: {e}')
        raise
    finally:
        stop_event.set()
        snapshot_worker_stop_event.set()
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

    if USE_QT_PHASE13_DASHBOARD:
        fig2 = Figure(figsize=(18, 15))
        FigureCanvas(fig2)
        ((fax_thickness_ck1, fax_rate_ck1, fax_pressure_xgs600),
         (fax_thickness_sample, fax_rate_sample, fax_temperature_oven),
         (fax_current_keysight, fax_voltage_keysight, fax_temperature_ck1)) = fig2.subplots(3, 3)
    else:
        fig2, ((fax_thickness_ck1, fax_rate_ck1, fax_pressure_xgs600),
               (fax_thickness_sample, fax_rate_sample, fax_temperature_oven),
               (fax_current_keysight, fax_voltage_keysight, fax_temperature_ck1)) = plt.subplots(3, 3, figsize=(18, 15))

    fig2.suptitle('Heat up + Calibration parameters', fontsize=16, fontweight='bold')
    fig2.subplots_adjust(left=0.05, right=0.99, top=0.9, bottom=0.1, hspace=0.45, wspace=0.25)

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
    fax_temperature_oven.plot(data['Oven PID temperature']['temperature_times'], data['Oven PID temperature']['temperature_data'], '-', color=TEMPERATURE_VIEW_STYLES['oven']['accent'], linewidth=1.7, label='Oven PID')
    fax_temperature_oven.plot(data['IMPAC pyrometer']['temperature_times'], data['IMPAC pyrometer']['temperature_data'], '-', color=TEMPERATURE_VIEW_STYLES['pyrometer']['accent'], linewidth=1.7, label='Pyrometer raw')
    fax_temperature_oven.plot(data['IMPAC pyrometer']['temperature_times'], data['IMPAC pyrometer']['sample_temperature_data'], '-', color=TEMPERATURE_VIEW_STYLES['sample']['accent'], linewidth=1.7, label='Sample estimate')
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
        if USE_QT_PHASE13_DASHBOARD:
            pass
        elif process_state.get('gui_auto_close', False):
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
