"""Run-only automation parameter definitions and validation.

The graphical launcher serializes only values that differ from the packaged
script defaults.  The selected phase receives those values through one JSON
environment variable.  Nothing is written back into the Python source files,
so closing the launcher restores the original defaults automatically.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

AUTOMATION_PARAMETERS_ENV = "NPG_CHAMBER_AUTOMATION_PARAMETERS_JSON"
PYROMETER_PARAMETERS_ENV = "NPG_CHAMBER_PYROMETER_PARAMETERS_JSON"
AUTOMATION_MODE_NAME_ENV = "NPG_CHAMBER_AUTOMATION_MODE_NAME"


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    label: str
    default: Any
    kind: str = "float"  # float | int | bool | choice | str
    unit: str = ""
    group: str = "General"
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    display_scale: float = 1.0  # internal value = displayed value * display_scale

    def display_value(self, internal_value: Any) -> Any:
        if self.kind in {"float", "int"}:
            return float(internal_value) / self.display_scale
        return internal_value

    def internal_value(self, displayed_value: Any) -> Any:
        if self.kind == "bool":
            return bool(displayed_value)
        if self.kind == "choice":
            value = str(displayed_value).strip()
            if value not in self.choices:
                raise ValueError(f"must be one of: {', '.join(self.choices)}")
            return value
        if self.kind == "str":
            value = str(displayed_value).strip()
            if not value:
                raise ValueError("cannot be empty")
            return value
        if self.kind == "int":
            number = float(str(displayed_value).strip().replace(",", "."))
            if not number.is_integer():
                raise ValueError("must be a whole number")
            return int(number * self.display_scale)
        number = float(str(displayed_value).strip().replace(",", "."))
        return number * self.display_scale

    def format_display(self, internal_value: Any) -> str:
        value = self.display_value(internal_value)
        if self.kind == "bool":
            return "true" if value else "false"
        if self.kind in {"choice", "str"}:
            return str(value)
        if self.kind == "int":
            return str(int(round(float(value))))
        return f"{float(value):.12g}"


# COM ports, baud rates, plotting, logging and fixed equipment hard stops remain
# excluded. The editor includes the operator-approved run limits that need to be
# reviewed before Phases 01 and 03, including the watchdog maximum temperature
# and the normal automatic-current cap.
def _ck1_common(*, include_calibration_target: bool, include_rampdown: bool = True) -> tuple[ParameterSpec, ...]:
    specs: list[ParameterSpec] = [
        ParameterSpec("HEATING_TRIGGER_TEMP_C", "CK-1 temperature target / guide", 242.0, unit="°C", group="Process targets", description="Temperature PID target in temperature mode. In rate and compound modes it remains a visual process guide while rate feedback becomes primary.", minimum=0, maximum=450),
        ParameterSpec("CK1_RATE_TARGET_A_PER_S", "CK-1 rate target", 0.40, unit="Å/s", group="Process targets", description="Target CK-1 QMB deposition rate. It is the minimum shutter-opening rate in temperature mode and the feedback setpoint in rate/compound modes.", minimum=0.001, maximum=5),
        ParameterSpec("CK1_RATE_AVG_WINDOW_POINTS", "Rate averaging points", 8, kind="int", group="Process targets", description="Number of recent CK-1 rate readings used for the shutter-opening average and rate feedback.", minimum=1, maximum=200),
        ParameterSpec("EVAPORATION_CONTROL_MODE", "Evaporation feedback mode", "temperature", kind="choice", choices=("temperature", "rate", "compound"), group="Evaporation feedback control", description="temperature uses the CK-1 temperature loop; rate is a conservative direct-current diagnostic loop; compound is the recommended true cascade mode: robust QMB rate adjusts the CK-1 temperature target and the temperature PID alone commands current."),
        ParameterSpec("MOLECULE_CONDITION_PROFILE", "Molecule condition profile", "normal", kind="choice", choices=("normal", "fresh_post_refill"), group="Evaporation feedback control", description="Select fresh_post_refill after replenishing the crucible. It slows the outer rate loop and extends settling time without weakening safety limits."),
        ParameterSpec("FRESH_PROFILE_INITIAL_TARGET_OFFSET_C", "Fresh-profile initial temperature offset", -4.0, unit="°C", group="Evaporation feedback control", description="Initial compound temperature target relative to the operator guide after a refill. The data-tuned default starts 4 °C lower to avoid reproducing the observed high-gain overshoot.", minimum=-30, maximum=0),
        ParameterSpec("FRESH_PROFILE_CASCADE_KI_SCALE", "Fresh-profile outer integral scale", 0.0, group="Cascade compound control", description="Multiplier applied to the cascade integral gain in fresh_post_refill. Zero keeps the fresh profile proportional-only until hardware validation confirms a need for integral action.", minimum=0, maximum=1),
        ParameterSpec("CONTROL_TEMPERATURE_FILTER_POINTS", "Control temperature median points", 5, kind="int", group="Temperature PID", description="Newest Arduino CK-1 readings used by feedback control. The independent watchdog continues to inspect raw readings.", minimum=3, maximum=21),
        ParameterSpec("RATE_CONTROL_MAX_TEMP_C", "Rate-control temperature ceiling", 250.0, unit="°C", group="Evaporation feedback control", description="Maximum CK-1 process temperature used by rate/compound control. Positive rate-PID corrections are blocked at this ceiling, while the independent watchdog maximum remains a separate higher safety stop.", minimum=0, maximum=450),
        ParameterSpec("TEMP_WATCHDOG_MAX_TEMP_C", "Watchdog maximum temperature", 255.0, unit="°C", group="Safety limits", description="Absolute CK-1 temperature at which the independent watchdog forces Keysight current to 0 A, switches output OFF and enters SAFETY_STOP. It must remain above the process target and the rate-control temperature ceiling.", minimum=0, maximum=500),
        ParameterSpec("RATE_PID_MIN_CONTROL_TEMP_C", "Minimum temperature for rate PID", 150.0, unit="°C", group="Evaporation feedback control", description="Rate PID cannot take control below this CK-1 temperature, reducing the risk of reacting to low-temperature QMB noise.", minimum=0, maximum=450),
        ParameterSpec("RATE_PID_ACTIVATION_A_PER_S", "Rate PID activation threshold", 0.05, unit="Å/s", group="Evaporation feedback control", description="Filtered CK-1 rate required before rate feedback takes control from the selected warm-up ramp.", minimum=0.0, maximum=5),
        ParameterSpec("RATE_PID_FILTER_POINTS", "Fast rate guard points", 11, kind="int", group="Rate estimation", description="Newest instantaneous rate readings used by the separate fast excursion guard and fallback diagnostics.", minimum=5, maximum=101),
        ParameterSpec("RATE_ESTIMATOR_WINDOW_S", "Thickness-slope rate window", 60.0, unit="s", group="Rate estimation", description="Time window for robust linear regression of CK-1 thickness versus time.", minimum=10, maximum=300),
        ParameterSpec("RATE_ESTIMATOR_MIN_POINTS", "Minimum thickness points", 30, kind="int", group="Rate estimation", description="Minimum number of thickness readings required before rate feedback acts.", minimum=5, maximum=300),
        ParameterSpec("RATE_ESTIMATOR_MIN_SPAN_S", "Minimum estimator time span", 45.0, unit="s", group="Rate estimation", description="Minimum elapsed time represented by the thickness regression.", minimum=5, maximum=300),
        ParameterSpec("RATE_ESTIMATOR_MIN_R2", "Minimum thickness-fit R²", 0.80, group="Rate estimation", description="Minimum linear-fit quality accepted for normal rate feedback.", minimum=-1, maximum=1),
        ParameterSpec("QMB_MAX_ABS_RATE_A_PER_S", "Maximum plausible absolute QMB rate", 10.0, unit="Å/s", group="QMB data quality", description="Readings outside this generous physical plausibility limit are excluded from control, plots and saved primary telemetry, and are written to the data-quality event log.", minimum=0.1, maximum=1000),
        ParameterSpec("QMB_MAX_DERIVED_THICKNESS_RATE_A_PER_S", "Maximum plausible thickness change rate", 10.0, unit="Å/s", group="QMB data quality", description="Maximum physically plausible rate inferred from consecutive thickness readings.", minimum=0.1, maximum=1000),
        ParameterSpec("QMB_MIN_ALLOWED_THICKNESS_JUMP_A", "Minimum allowed thickness jump margin", 5.0, unit="Å", group="QMB data quality", description="Extra jump allowance used with the elapsed-time rate limit to avoid rejecting harmless quantization while still rejecting resets and communication corruption.", minimum=0.01, maximum=1000),
        ParameterSpec("RATE_PID_CONTROL_PERIOD_S", "Outer rate control period", 60.0, unit="s", group="Rate PID", description="Minimum time between direct-rate or cascade outer-loop evaluations.", minimum=2, maximum=180),
        ParameterSpec("RATE_CONTROL_SETTLING_S", "Settling time after rate correction", 180.0, unit="s", group="Rate PID", description="Minimum observation time after an outer-loop action. The controller can extend the hold automatically until the inner temperature loop has physically settled.", minimum=0, maximum=600),
        ParameterSpec("CASCADE_INNER_READY_TEMP_BAND_C", "Inner-loop target band", 0.75, unit="°C", group="Cascade compound control", description="The outer rate loop is not allowed to request further heating until CK-1 temperature is this close to its active cascade target.", minimum=0.1, maximum=10),
        ParameterSpec("CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN", "Inner-loop maximum temperature slope", 0.30, unit="°C/min", group="Cascade compound control", description="Maximum absolute CK-1 temperature slope accepted before the compound cascade outer loop may stack another action. This is controller-internal and never gates shutter opening.", minimum=0.01, maximum=10),
        ParameterSpec("CASCADE_INNER_READY_STABLE_DURATION_S", "Inner-loop stable duration", 60.0, unit="s", group="Cascade compound control", description="Continuous time that temperature must remain close to target and nearly stationary before a low-rate condition may raise the target again. The fresh profile doubles this duration.", minimum=0, maximum=900),
        ParameterSpec("CASCADE_THERMAL_RESPONSE_MAX_HOLD_S", "Maximum thermal propagation hold", 420.0, unit="s", group="Cascade compound control", description="Maximum time to wait while a previous cascade action is still propagating through CK-1 temperature. The fresh profile extends the effective maximum to 600 s.", minimum=60, maximum=1800),
        ParameterSpec("RATE_PID_DEADBAND_A_PER_S", "Rate PID dead band", 0.03, unit="Å/s", group="Rate PID", description="No-current-change band around the requested rate, used to avoid chasing QMB noise.", minimum=0.0, maximum=1.0),
        ParameterSpec("RATE_PID_KP_A_PER_RATE", "Rate PID Kp", 0.020, unit="A/(Å/s)", group="Rate PID", description="Proportional current correction gain for CK-1 rate error.", minimum=0, maximum=1),
        ParameterSpec("RATE_PID_KI_A_PER_THICKNESS", "Rate PID Ki", 0.00020, unit="A/Å", group="Rate PID", description="Integral gain; rate error integrated over time has thickness units.", minimum=0, maximum=0.1),
        ParameterSpec("RATE_PID_KD_A_PER_RATE_SLOPE", "Rate PID Kd", 0.0, unit="A/(Å/s²)", group="Rate PID", description="Derivative gain. The conservative packaged value is zero because QMB rate is noisy.", minimum=0, maximum=10),
        ParameterSpec("RATE_PID_MAX_UP_STEP_A", "Direct-rate maximum increase", 0.0010, unit="A", group="Rate PID", description="Maximum current increase per direct-rate diagnostic update.", minimum=0.00001, maximum=0.05),
        ParameterSpec("RATE_PID_MAX_DOWN_STEP_A", "Direct-rate maximum decrease", 0.0015, unit="A", group="Rate PID", description="Maximum current decrease per direct-rate diagnostic update. Large post-refill corrections are deliberately avoided.", minimum=0.00001, maximum=0.1),
        ParameterSpec("CASCADE_RATE_KP_C_PER_RATE", "Cascade outer Kp", 1.5, unit="°C/(Å/s)", group="Cascade compound control", description="Proportional temperature-target correction per rate error.", minimum=0, maximum=100),
        ParameterSpec("CASCADE_RATE_KI_C_PER_THICKNESS", "Cascade outer Ki", 0.0025, unit="°C/Å", group="Cascade compound control", description="Slow integral gain for residual rate error.", minimum=0, maximum=10),
        ParameterSpec("CASCADE_MAX_TARGET_UP_C_PER_MIN", "Maximum target increase", 0.40, unit="°C/min", group="Cascade compound control", description="Maximum upward CK-1 target slew from the outer loop.", minimum=0.01, maximum=20),
        ParameterSpec("CASCADE_MAX_TARGET_DOWN_C_PER_MIN", "Maximum target decrease", 0.40, unit="°C/min", group="Cascade compound control", description="Maximum downward CK-1 target slew from the outer loop.", minimum=0.01, maximum=20),
        ParameterSpec("CASCADE_MAX_TARGET_STEP_C", "Maximum temperature-target action", 1.0, unit="°C", group="Cascade compound control", description="Hard cap on one outer-loop target correction after the settling interval. The fresh profile applies a tighter effective cap.", minimum=0.05, maximum=10),
        ParameterSpec("FRESH_PROFILE_MAX_TARGET_STEP_C", "Fresh-profile maximum target action", 0.75, unit="°C", group="Cascade compound control", description="Maximum one-action temperature-target change in fresh_post_refill. It permits useful progress after a full settling observation while remaining conservative.", minimum=0.05, maximum=5),
        ParameterSpec("CASCADE_TARGET_TRIM_LIMIT_C", "Maximum cascade temperature trim", 8.0, unit="°C", group="Cascade compound control", description="Maximum target offset above or below the operator guide. The absolute temperature ceiling still applies.", minimum=0.1, maximum=100),
        ParameterSpec("CASCADE_INTEGRAL_LIMIT_THICKNESS_A", "Cascade integral limit", 20.0, unit="Å", group="Cascade compound control", description="Anti-windup bound for integrated rate error.", minimum=0, maximum=10000),
        ParameterSpec("CASCADE_TREND_HOLD_THRESHOLD_A_PER_S2", "Rate trend hold threshold", 0.0005, unit="Å/s²", group="Cascade compound control", description="If rate is already moving toward target faster than this slope, wait rather than correcting again.", minimum=0, maximum=1),
        ParameterSpec("RATE_ESTIMATE_TREND_WINDOW_S", "Estimated-rate trend window", 180.0, unit="s", group="Cascade compound control", description="Window used to determine whether the robust thickness-slope rate is already moving toward target. It is intentionally comparable to the measured current-to-rate delay.", minimum=60, maximum=900),
        ParameterSpec("RATE_ESTIMATE_TREND_MIN_POINTS", "Minimum estimated-rate trend points", 3, kind="int", group="Cascade compound control", description="Minimum number of robust rate estimates required before trend-hold logic is allowed to act.", minimum=3, maximum=100),
        ParameterSpec("FAST_RATE_EXCURSION_FACTOR", "Fast high-rate guard factor", 1.75, group="Rate protection", description="A sustained instantaneous rate above target multiplied by this factor activates a conservative protection action.", minimum=1.05, maximum=10),
        ParameterSpec("FAST_RATE_EXCURSION_DURATION_S", "Fast guard persistence", 20.0, unit="s", group="Rate protection", description="High-rate condition must persist for this time before protective action.", minimum=1, maximum=300),
        ParameterSpec("FAST_RATE_EXCURSION_CURRENT_STEP_A", "Fast guard current reduction", 0.0010, unit="A", group="Rate protection", description="Single conservative current reduction used by the exceptional high-rate guard.", minimum=0.0001, maximum=0.01),
        ParameterSpec("RATE_PID_INTEGRAL_LIMIT_THICKNESS_A", "Rate PID integral limit", 25.0, unit="Å", group="Rate PID", description="Anti-windup limit for accumulated rate error.", minimum=0, maximum=10000),
        ParameterSpec("RATE_PID_SIGNAL_TIMEOUT_S", "Rate feedback timeout", 30.0, unit="s", group="Rate PID", description="After rate feedback has taken control, a QMB rate signal older than this causes a safe electrical stop.", minimum=2, maximum=600),
        ParameterSpec("COMPOUND_TEMP_GUARD_BAND_C", "Compound temperature guard band", 5.0, unit="°C", group="Rate PID", description="Positive rate-PID corrections are progressively reduced within this distance below the rate-control temperature ceiling.", minimum=0, maximum=100),
        ParameterSpec("KEYSIGHT_SOFT_WARNING_A", "Maximum automatic current cap", 0.660, unit="A", group="Safety limits", description="Highest current that automatic ramp, temperature PID and rate PID control may command. The fixed 0.680 A software hard stop remains higher and is not editable here.", minimum=0.001, maximum=0.675),
        ParameterSpec("KEYSIGHT_START_CURRENT_A", "First ramp current", 0.005, unit="A", group="Keysight ramp-up", description="First ramp setpoint applied only after the Keysight output has been enabled and verified at 0 A.", minimum=0, maximum=0.675),
        ParameterSpec("KEYSIGHT_BASE_WORK_CURRENT_A", "Base working current", 0.640, unit="A", group="Keysight ramp-up", description="Working-current reference used by the ramp controller.", minimum=0.001, maximum=0.675),
        ParameterSpec("KEYSIGHT_STEP_A", "Current step", 0.005, unit="A", group="Keysight ramp-up", description="Fixed current increment used by step-based ramping.", minimum=0.0001, maximum=0.05),
        ParameterSpec("KEYSIGHT_STEP_PERIOD_S", "General step period", 15.0, unit="s", group="Keysight ramp-up", description="Default delay between automatic current steps.", minimum=0.1, maximum=600),
        ParameterSpec("DEFAULT_RAMP_UP_MODE", "Default ramp-up mode", "steps", kind="choice", choices=("steps", "slope"), group="Keysight ramp-up", description="Ramp strategy selected when the phase starts."),
        ParameterSpec("STEPS_RAMP_UNTIL_TEMP_C", "Step → slope transition temperature", 100.0, unit="°C", group="Keysight ramp-up", description="CK-1 temperature at which the automatic warm-up leaves fixed current steps and hands over to slope-controlled ramping.", minimum=0, maximum=450),
        ParameterSpec("STEPS_RAMP_STEP_PERIOD_S", "Step mode period", 15.0, unit="s", group="Keysight ramp-up", description="Delay between current increments in step mode.", minimum=0.1, maximum=600),
        ParameterSpec("PID_CONTROL_PERIOD_S", "PID control period", 8.0, unit="s", group="Temperature PID", description="Time between CK-1 temperature PID corrections.", minimum=0.1, maximum=120),
        ParameterSpec("PID_TEMP_BAND_C", "PID temperature band", 0.7, unit="°C", group="Temperature PID", description="Dead band around the CK-1 target temperature.", minimum=0, maximum=50),
        ParameterSpec("PID_KP_A_PER_C", "PID Kp", 0.0020, unit="A/°C", group="Temperature PID", description="Proportional gain for CK-1 temperature control.", minimum=0, maximum=0.1),
        ParameterSpec("PID_KI_A_PER_C_S", "PID Ki", 0.000030, unit="A/(°C·s)", group="Temperature PID", description="Integral gain for CK-1 temperature control.", minimum=0, maximum=0.01),
        ParameterSpec("PID_KD_A_PER_C_PER_S", "PID Kd", 0.0, unit="A/(°C/s)", group="Temperature PID", description="Derivative-on-measurement gain. Packaged as zero until filtered derivative behaviour is validated on hardware.", minimum=0, maximum=1),
        ParameterSpec("PID_INTEGRAL_LIMIT_C_S", "PID integral limit", 250.0, unit="°C·s", group="Temperature PID", description="Absolute anti-windup limit for accumulated PID error.", minimum=0, maximum=100000),
        ParameterSpec("PID_INTEGRAL_ACTIVE_ERROR_C", "PID integral activation error", 5.0, unit="°C", group="Temperature PID", description="Integral action is enabled only inside this error range to prevent heat-up windup.", minimum=0.1, maximum=100),
        ParameterSpec("PID_DERIVATIVE_FILTER_TAU_S", "Derivative filter time constant", 20.0, unit="s", group="Temperature PID", description="Low-pass time constant for derivative-on-measurement.", minimum=0, maximum=600),
        ParameterSpec("PID_MAX_UP_SLEW_A_PER_MIN", "Maximum PID current increase", 0.01875, unit="A/min", group="Temperature PID", description="Time-normalized positive current slew limit.", minimum=0.0001, maximum=1),
        ParameterSpec("PID_MAX_DOWN_SLEW_A_PER_MIN", "Maximum PID current decrease", 0.01875, unit="A/min", group="Temperature PID", description="Time-normalized negative current slew limit.", minimum=0.0001, maximum=1),
        ParameterSpec("TEMP_SLOPE_WINDOW_POINTS", "Minimum slope points", 15, kind="int", group="Slope ramp", description="Minimum CK-1 readings required by the robust time-window slope fit.", minimum=2, maximum=500),
        ParameterSpec("TEMP_SLOPE_WINDOW_S", "Slope estimation window", 45.0, unit="s", group="Slope ramp", description="Time window used for robust temperature-slope regression.", minimum=10, maximum=300),
        ParameterSpec("TEMP_SLOPE_MIN_SPAN_S", "Minimum slope time span", 20.0, unit="s", group="Slope ramp", description="Minimum elapsed time required before slope ramping can change current.", minimum=5, maximum=300),
        ParameterSpec("TEMP_SLOPE_TARGET_EARLY_C_PER_MIN", "Early slope target", 9.0, unit="°C/min", group="Slope ramp", description="Target heating slope in the early-current region.", minimum=0, maximum=100),
        ParameterSpec("TEMP_SLOPE_TARGET_MID_C_PER_MIN", "Middle slope target", 8.0, unit="°C/min", group="Slope ramp", description="Target heating slope in the middle-current region.", minimum=0, maximum=100),
        ParameterSpec("TEMP_SLOPE_TARGET_LATE_C_PER_MIN", "Late slope target", 7.0, unit="°C/min", group="Slope ramp", description="Target heating slope in the late-current region.", minimum=0, maximum=100),
        ParameterSpec("TEMP_SLOPE_DEADBAND_C_PER_MIN", "Slope dead band", 0.20, unit="°C/min", group="Slope ramp", description="Slope error band in which no correction is made.", minimum=0, maximum=20),
        ParameterSpec("TEMP_SLOPE_KP_A_PER_C_PER_MIN", "Slope controller Kp", 0.010, unit="A/(°C/min)", group="Slope ramp", description="Current correction gain for slope-based ramping.", minimum=0, maximum=1),
        ParameterSpec("FAST_RAMP_CURRENT_THRESHOLD_A", "Early/middle current boundary", 0.50, unit="A", group="Slope ramp", description="Current at which the slope controller changes from early to middle settings.", minimum=0, maximum=0.680),
        ParameterSpec("MID_RAMP_CURRENT_THRESHOLD_A", "Middle/late current boundary", 0.60, unit="A", group="Slope ramp", description="Current at which the slope controller changes from middle to late settings.", minimum=0, maximum=0.680),
        ParameterSpec("EARLY_RAMP_MAX_STEP_A", "Early maximum step", 0.005, unit="A", group="Slope ramp", description="Maximum slope-controller current correction in the early region.", minimum=0.00001, maximum=0.05),
        ParameterSpec("MID_RAMP_MAX_STEP_A", "Middle maximum step", 0.005, unit="A", group="Slope ramp", description="Maximum slope-controller current correction in the middle region.", minimum=0.00001, maximum=0.05),
        ParameterSpec("LATE_RAMP_MAX_STEP_A", "Late maximum step", 0.005, unit="A", group="Slope ramp", description="Maximum slope-controller current correction in the late region.", minimum=0.00001, maximum=0.05),
        ParameterSpec("RAMPDOWN_STEP_A", "Ramp-down current step", 0.010, unit="A", group="Ramp-down", description="Current reduction per safe ramp-down action.", minimum=0.0001, maximum=0.1),
        ParameterSpec("RAMPDOWN_STEP_PERIOD_S", "Ramp-down step period", 15, kind="int", unit="s", group="Ramp-down", description="Delay between current reductions during ramp-down.", minimum=1, maximum=3600),
        ParameterSpec("RAMPDOWN_ZERO_THRESHOLD_A", "Ramp-down zero threshold", 0.003, unit="A", group="Ramp-down", description="Current below which the output is treated as effectively zero.", minimum=0, maximum=0.05),
    ]
    if include_calibration_target:
        specs.insert(3, ParameterSpec("CALIBRATION_TARGET_SAMPLE_A", "Sample calibration target", 2.0, unit="Å", group="Process targets", description="Sample-relative thickness that must remain reached for the fixed 5 s confirmation window before Phase 01 calibration ends.", minimum=0.001, maximum=100))
        specs.insert(6, ParameterSpec("CALIBRATION_MIN_LINEAR_R2", "Minimum calibration linearity R²", 0.985, group="Calibration quality", description="Minimum linearity of CK-1 thickness versus sample thickness during the open-shutter calibration interval.", minimum=0.0, maximum=1.0))
    if not include_rampdown:
        obsolete = {"RAMPDOWN_STEP_A", "RAMPDOWN_STEP_PERIOD_S", "RAMPDOWN_ZERO_THRESHOLD_A"}
        specs = [spec for spec in specs if spec.key not in obsolete]
    return tuple(specs)


PHASE_PARAMETER_SPECS: dict[str, tuple[ParameterSpec, ...]] = {
    "heat": _ck1_common(include_calibration_target=True),
    "sputter": (
        ParameterSpec("cycles", "Number of cycles", 3, kind="int", group="Workflow", description="Number of sputtering-annealing cycles.", minimum=1, maximum=100),
        ParameterSpec(
            "start_without_degassing",
            "Start without initial Degas",
            False,
            kind="bool",
            group="Workflow",
            description=(
                "Skip the automatic COSCON Degas before cycle 1. Use only when continuing the same chamber preparation "
                "after an earlier partial Phase 02 run and the operator has verified that a new Degas is not required."
            ),
        ),
        ParameterSpec("degas_timeout_minutes", "Degas safety timeout", 25.0, unit="min", group="Workflow", description="Maximum allowed wait for COSCON Degas to finish naturally in Standby. This is a safety timeout, not the expected Degas duration.", minimum=5, maximum=60),
        ParameterSpec("sputter_minutes", "Sputtering duration", 20.0, unit="min", group="Workflow", description="Countdown duration for each sputtering step.", minimum=0, maximum=1440),
        ParameterSpec("coscon_energy_v", "COSCON energy target", 2250.0, unit="V", group="COSCON sputtering target", description="Beam-energy target sent to ValidateOperateTarget and SwitchToOperate. The packaged default is 2250 V.", minimum=100, maximum=3000),
        ParameterSpec("coscon_emission_a", "COSCON emission target", 0.010, unit="mA", group="COSCON sputtering target", description="Electron-emission target sent to ValidateOperateTarget and SwitchToOperate. The packaged default is 10.0 mA.", minimum=0.002, maximum=0.020, display_scale=0.001),
        ParameterSpec("coscon_energy_tolerance_v", "Energy tolerance", 50.0, unit="V", group="COSCON safety margins", description="Immediate-abort tolerance around the requested COSCON beam energy.", minimum=1, maximum=500),
        ParameterSpec("coscon_emission_tolerance_a", "Emission tolerance", 0.001, unit="mA", group="COSCON safety margins", description="Allowed deviation around the emission target before a reading is counted as anomalous.", minimum=0.00001, maximum=0.010, display_scale=0.001),
        ParameterSpec("coscon_emission_fault_samples", "Bad emission reads before abort", 3, kind="int", group="COSCON safety margins", description="Consecutive out-of-tolerance emission measurements required before safe abort.", minimum=1, maximum=20),
        ParameterSpec("coscon_emission_recheck_s", "Emission recheck delay", 0.5, unit="s", group="COSCON safety margins", description="Delay before repeating an anomalous emission measurement.", minimum=0.1, maximum=10),
        ParameterSpec("coscon_stable_samples", "Stable output reads", 5, kind="int", group="COSCON safety margins", description="Consecutive valid energy/emission readings required before sputtering starts.", minimum=1, maximum=50),
        ParameterSpec("coscon_activation_overload_retries", "Activation overload retries", 1, kind="int", group="COSCON safety margins", description="Short automatic retries allowed only for HV-Module Energy Overload during COSCON activation. The packaged default preserves one verified 8-second retry before Reset recovery is considered.", minimum=0, maximum=2),
        ParameterSpec("coscon_activation_recovery_wait_s", "Activation recovery wait", 8.0, unit="s", group="COSCON safety margins", description="Verified safe-state wait before re-validating and retrying COSCON Operate after a transient activation energy overload.", minimum=2, maximum=60),
        ParameterSpec("coscon_activation_reset_retries", "Activation Reset recoveries", 1, kind="int", group="COSCON safety margins", description="Documented COSCON controller Reset recoveries allowed after the exact activation overload repeats. The valve is closed first and only one Reset is permitted by default.", minimum=0, maximum=1),
        ParameterSpec("coscon_reset_reconnect_timeout_s", "Reset reconnect timeout", 60.0, unit="s", group="COSCON safety margins", description="Maximum time to re-establish UDP communication and verify safe Off after the documented Reset.", minimum=15, maximum=180),
        ParameterSpec("coscon_reset_safe_samples", "Safe Off reads after Reset", 3, kind="int", group="COSCON safety margins", description="Consecutive Mode=Off, Interlock=Ok and de-energized monitor readings required after Reset.", minimum=2, maximum=10),
        ParameterSpec("coscon_reset_safe_sample_interval_s", "Post-Reset safe-read interval", 2.0, unit="s", group="COSCON safety margins", description="Delay between consecutive post-Reset safe-state readings.", minimum=0.2, maximum=10),
        ParameterSpec("coscon_post_reset_conditioning_s", "Post-Reset pressure conditioning", 60.0, unit="s", group="COSCON safety margins", description="Pressure-conditioning time after the valve is reopened and before the final Operate attempt.", minimum=10, maximum=300),
        ParameterSpec("anneal_target_c", "Annealing target", 620.0, unit="°C", group="Annealing", description="Oven PID setpoint used for each annealing stage.", minimum=0, maximum=750),
        ParameterSpec("anneal_hold_minutes", "Annealing hold", 10.0, unit="min", group="Annealing", description="Hold duration after the oven reaches the target window.", minimum=0, maximum=1440),
        ParameterSpec("anneal_reset_c", "PID reset after cycle", 0.0, unit="°C", group="Annealing", description="PID setpoint written after each annealing hold.", minimum=0, maximum=750),
        ParameterSpec("abort_reset_c", "PID reset on abort", 0.0, unit="°C", group="Annealing", description="PID setpoint attempted during an abort. The packaged safe-stop default is 0 °C.", minimum=0, maximum=750),
        ParameterSpec("target_ar_pressure_mbar", "Target Ar pressure", 2.0e-5, unit="mbar", group="Pressure guidance", description="Operator guidance target for argon pressure.", minimum=1e-12, maximum=1),
        ParameterSpec("pressure_warning_mbar", "Pressure warning threshold", 3.0e-5, unit="mbar", group="Pressure guidance", description="Pressure above which the interface shows a warning.", minimum=1e-12, maximum=1),
        ParameterSpec("pressure_emergency_mbar", "Emergency pressure limit", 1.0e-4, unit="mbar", group="Pressure guidance", description="Software emergency limit that immediately stops COSCON operation.", minimum=1e-12, maximum=1),
        ParameterSpec("temperature_reach_tolerance_c", "Target stability tolerance", 5.0, unit="°C", group="Temperature detection", description="Symmetric oven PV band around target before and during holds.", minimum=0.1, maximum=100),
        ParameterSpec("temperature_stable_duration_s", "Stable time before hold", 30.0, unit="s", group="Temperature detection", description="PV must remain continuously inside the target band before the hold starts.", minimum=2, maximum=3600),
        ParameterSpec("pause_hold_outside_temperature_band", "Pause hold outside band", True, kind="bool", group="Temperature detection", description="Count annealing time only while PV remains in band."),
        ParameterSpec("try_reset_pid_on_abort", "Reset PID on abort", True, kind="bool", group="Abort behaviour", description="Attempt to write the abort reset setpoint during abort."),
    ),
    "dpdbba": (
        ParameterSpec("DP_DBBA_SAMPLE_EQUIVALENT_THICKNESS_A", "DP-DBBA sample-equivalent target", 623.13 / 94.39, unit="Å", group="Process targets", description="Sample-equivalent amount multiplied by the Phase 01 ratio to obtain the CK-1 target.", minimum=0.001, maximum=1000),
        ParameterSpec("OVEN_TARGET_TEMPERATURE_C", "Oven PID target at startup", 200.0, unit="°C", group="Process targets", description="External oven PID setpoint written when Phase 03 starts.", minimum=0, maximum=750),
        ParameterSpec("OVEN_READY_TOLERANCE_C", "Oven readiness tolerance", 2.0, unit="°C", group="Process targets", description="Symmetric PV band around 200 °C required before shutter opening.", minimum=0.1, maximum=50),
        ParameterSpec("OVEN_READY_STABLE_DURATION_S", "Oven stable time", 60.0, unit="s", group="Process targets", description="Time the external oven must remain in its readiness band before shutter opening.", minimum=5, maximum=3600),
        ParameterSpec("OVEN_READY_SIGNAL_TIMEOUT_S", "Oven signal timeout", 10.0, unit="s", group="Process targets", description="Maximum age of the external oven PV accepted for Phase 03 readiness.", minimum=2, maximum=300),
        *_ck1_common(include_calibration_target=False, include_rampdown=False),
    ),
    "anneal": (
        ParameterSpec("INITIAL_WAIT_S", "Initial wait", 5 * 60, unit="min", group="Initial stage", description="Time held at the initial oven target.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("INITIAL_WAIT_TARGET_C", "Initial wait target", 200.0, unit="°C", group="Initial stage", description="PID setpoint during the initial wait.", minimum=0, maximum=750),
        ParameterSpec("FIRST_STAGE_TARGET_C", "First-stage target", 350.0, unit="°C", group="Annealing recipe", description="First NPG annealing temperature.", minimum=0, maximum=750),
        ParameterSpec("FIRST_STAGE_HOLD_S", "First-stage hold", 15 * 60, unit="min", group="Annealing recipe", description="Hold duration at the first-stage temperature.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("SECOND_STAGE_TARGET_C", "Second-stage target", 600.0, unit="°C", group="Annealing recipe", description="Second NPG annealing temperature.", minimum=0, maximum=750),
        ParameterSpec("SECOND_STAGE_HOLD_S", "Second-stage hold", 40 * 60, unit="min", group="Annealing recipe", description="Hold duration at the second-stage temperature.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("STAGE_REACHED_MARGIN_C", "Stage stability tolerance", 2.0, unit="°C", group="Annealing recipe", description="Symmetric PV band around each annealing target.", minimum=0.1, maximum=100),
        ParameterSpec("STAGE_STABLE_DURATION_S", "Stable time before hold", 30.0, unit="s", group="Annealing recipe", description="PV must remain continuously in the target band before a hold starts.", minimum=2, maximum=3600),
        ParameterSpec("PAUSE_HOLD_OUTSIDE_TEMPERATURE_BAND", "Pause hold outside band", True, kind="bool", group="Annealing recipe", description="Count first/second-stage hold time only while PV remains inside the target band."),
        ParameterSpec("OVEN_SIGNAL_STALE_TIMEOUT_S", "Oven PV stale timeout", 10.0, unit="s", group="Annealing recipe", description="Abort Phase 04 safely if the oven process-value signal remains unavailable or stale beyond this time.", minimum=2, maximum=300),
        ParameterSpec("COOLDOWN_TARGET_C", "Cooldown target", 0.0, unit="°C", group="Finalization", description="PID setpoint sent after the second hold.", minimum=0, maximum=750),
        ParameterSpec("POST_COOLDOWN_WAIT_S", "Final hold at cooldown target", 10 * 60, unit="min", group="Finalization", description="Time held at the cooldown target before Phase 04 finishes with that same PID setpoint.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("KEYSIGHT_RAMPDOWN_STEP_A", "Keysight ramp-down step", 0.005, unit="A", group="Keysight ramp-down", description="Current reduction per ramp-down step.", minimum=0.0001, maximum=0.1),
        ParameterSpec("KEYSIGHT_RAMPDOWN_STEP_S", "Keysight step period", 15, kind="int", unit="s", group="Keysight ramp-down", description="Delay between current reductions.", minimum=1, maximum=3600),
        ParameterSpec("FIRST_RAMPDOWN_STEP_DELAY_S", "First ramp-down delay", 10, kind="int", unit="s", group="Keysight ramp-down", description="Delay before the first current reduction.", minimum=0, maximum=3600),
        ParameterSpec("KEYSIGHT_ZERO_THRESHOLD_A", "Current zero threshold", 0.003, unit="A", group="Keysight ramp-down", description="Current below which the output is treated as zero.", minimum=0, maximum=0.05),
    ),
}


PYROMETER_PARAMETER_SPECS: tuple[ParameterSpec, ...] = (
    ParameterSpec(
        "enabled",
        "Enable pyrometer monitoring",
        True,
        kind="bool",
        group="Availability",
        description="Read COM10 during Phases 01, 03 and 04. A missing pyrometer is logged as unavailable and does not stop the phase.",
    ),
    ParameterSpec(
        "profile_name",
        "Calibration profile",
        "Au/mica — validated",
        kind="str",
        group="Material profile",
        description="Choose a saved material mode or create a new one. The validated Au/mica profile is read-only.",
    ),
    ParameterSpec(
        "emissivity_percent",
        "Instrument emissivity",
        10,
        kind="int",
        unit="%",
        group="Material profile",
        description="Whole-percent setting used by this IPE 140 display/protocol (for example 10, 11 or 35). It is verified from the parameter readback.",
        minimum=10,
        maximum=100,
    ),
    ParameterSpec(
        "sample_slope",
        "Sample calibration slope",
        1.69959,
        group="Sample-temperature calibration",
        description="Slope m in T_sample = m × T_pyro + b.",
        minimum=0.000001,
        maximum=100.0,
    ),
    ParameterSpec(
        "sample_intercept_c",
        "Sample calibration intercept",
        28.20193,
        unit="°C",
        group="Sample-temperature calibration",
        description="Intercept b in T_sample = m × T_pyro + b.",
        minimum=-5000.0,
        maximum=5000.0,
    ),
    ParameterSpec(
        "minimum_valid_pyrometer_c",
        "Minimum calibrated pyrometer temperature",
        90.0,
        unit="°C",
        group="Sample-temperature calibration",
        description="Below this raw value, T_sample is still calculated and displayed, but it is marked as an extrapolation warning.",
        minimum=50.0,
        maximum=1200.0,
    ),
    ParameterSpec(
        "write_emissivity_at_start",
        "Write and verify emissivity at startup",
        True,
        kind="bool",
        group="Instrument setup",
        description="Applies the selected emissivity before monitoring starts, then reads it back. Failure does not alter PID or Keysight control.",
    ),
    ParameterSpec(
        "default_view",
        "Default temperature view",
        "oven",
        kind="choice",
        choices=("oven", "pyrometer", "sample"),
        group="Display",
        description="Initial curve shown in the shared temperature graph. It can be changed live in Phases 01, 03 and 04.",
    ),
)


def pyrometer_specs() -> tuple[ParameterSpec, ...]:
    return PYROMETER_PARAMETER_SPECS


def pyrometer_default_values() -> dict[str, Any]:
    return {spec.key: spec.default for spec in PYROMETER_PARAMETER_SPECS}


def validate_pyrometer_values(values: Mapping[str, Any]) -> dict[str, Any]:
    known = {spec.key: spec for spec in PYROMETER_PARAMETER_SPECS}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"Unknown pyrometer parameter(s): {', '.join(unknown)}")
    normalized = pyrometer_default_values()
    for key, raw in values.items():
        spec = known[key]
        try:
            normalized[key] = _validate_one(spec, raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.label}: {exc}") from exc

    if normalized["profile_name"] == "Au/mica — validated":
        expected = pyrometer_default_values()
        calibrated_keys = (
            "emissivity_percent",
            "sample_slope",
            "sample_intercept_c",
            "minimum_valid_pyrometer_c",
        )
        for key in calibrated_keys:
            if abs(float(normalized[key]) - float(expected[key])) > 1e-9:
                raise ValueError(
                    "The Au/mica validated profile must keep its validated emissivity, "
                    "slope, intercept and minimum temperature. Select Custom material "
                    "before editing calibration values."
                )
    return normalized


def non_default_pyrometer_overrides(values: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_pyrometer_values(values)
    defaults = pyrometer_default_values()
    return {key: value for key, value in normalized.items() if value != defaults[key]}


def encode_pyrometer_settings(values: Mapping[str, Any]) -> str:
    normalized = validate_pyrometer_values(values)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def load_pyrometer_settings(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    raw = str(env.get(PYROMETER_PARAMETERS_ENV, "")).strip()
    if not raw:
        return pyrometer_default_values()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {PYROMETER_PARAMETERS_ENV}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid {PYROMETER_PARAMETERS_ENV}: expected a JSON object")
    return validate_pyrometer_values(payload)


def format_pyrometer_summary(values: Mapping[str, Any]) -> str:
    normalized = validate_pyrometer_values(values)
    view_label = {
        "oven": "Oven PID",
        "pyrometer": "Pyrometer raw",
        "sample": "Sample estimate",
    }[normalized["default_view"]]
    return (
        "Pyrometer run profile:\n"
        f"  - enabled: {normalized['enabled']}\n"
        f"  - profile: {normalized['profile_name']}\n"
        f"  - emissivity: {normalized['emissivity_percent']:.1f}%\n"
        f"  - T_sample = {normalized['sample_slope']:.8g} × T_pyro "
        f"+ {normalized['sample_intercept_c']:.8g} °C\n"
        f"  - calibrated for T_pyro ≥ {normalized['minimum_valid_pyrometer_c']:.1f} °C\n"
        f"  - default view: {view_label}"
    )


def specs_for_phase(phase: str) -> tuple[ParameterSpec, ...]:
    try:
        return PHASE_PARAMETER_SPECS[phase]
    except KeyError as exc:
        raise ValueError(f"Unknown phase parameter set: {phase!r}") from exc


def defaults_for_phase(phase: str) -> dict[str, Any]:
    return {spec.key: spec.default for spec in specs_for_phase(phase)}


def all_default_values() -> dict[str, dict[str, Any]]:
    return {phase: defaults_for_phase(phase) for phase in PHASE_PARAMETER_SPECS}


def _validate_one(spec: ParameterSpec, value: Any) -> Any:
    if spec.kind == "bool":
        if isinstance(value, bool):
            normalized = value
        elif str(value).strip().lower() in {"1", "true", "yes", "on"}:
            normalized = True
        elif str(value).strip().lower() in {"0", "false", "no", "off"}:
            normalized = False
        else:
            raise ValueError("must be true or false")
    elif spec.kind == "choice":
        normalized = str(value).strip()
        if normalized not in spec.choices:
            raise ValueError(f"must be one of: {', '.join(spec.choices)}")
    elif spec.kind == "str":
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("cannot be empty")
    elif spec.kind == "int":
        if isinstance(value, bool):
            raise ValueError("must be a whole number")
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError("must be a finite whole number")
        normalized = int(number)
    else:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        normalized = number

    if spec.kind in {"float", "int"}:
        numeric = float(normalized)
        if spec.minimum is not None and numeric < spec.minimum:
            shown_min = spec.minimum / spec.display_scale
            raise ValueError(f"must be at least {shown_min:g} {spec.unit}".strip())
        if spec.maximum is not None and numeric > spec.maximum:
            shown_max = spec.maximum / spec.display_scale
            raise ValueError(f"must be at most {shown_max:g} {spec.unit}".strip())
    return normalized


def validate_phase_values(phase: str, values: Mapping[str, Any]) -> dict[str, Any]:
    specs = specs_for_phase(phase)
    known = {spec.key: spec for spec in specs}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"Unknown automation parameter(s) for {phase}: {', '.join(unknown)}")

    normalized = defaults_for_phase(phase)
    for key, raw in values.items():
        spec = known[key]
        try:
            normalized[key] = _validate_one(spec, raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.label}: {exc}") from exc

    if phase in {"heat", "dpdbba"}:
        if normalized["KEYSIGHT_START_CURRENT_A"] > normalized["KEYSIGHT_BASE_WORK_CURRENT_A"]:
            raise ValueError("Starting current cannot be greater than base working current.")
        if normalized["KEYSIGHT_BASE_WORK_CURRENT_A"] > normalized["KEYSIGHT_SOFT_WARNING_A"]:
            raise ValueError("Base working current cannot exceed the maximum automatic current cap.")
        if normalized["FAST_RAMP_CURRENT_THRESHOLD_A"] >= normalized["MID_RAMP_CURRENT_THRESHOLD_A"]:
            raise ValueError("The early/middle current boundary must be below the middle/late boundary.")
        if normalized["MID_RAMP_CURRENT_THRESHOLD_A"] > normalized["KEYSIGHT_BASE_WORK_CURRENT_A"]:
            raise ValueError("The middle/late current boundary cannot exceed the base working current.")
        if normalized["STEPS_RAMP_UNTIL_TEMP_C"] > normalized["HEATING_TRIGGER_TEMP_C"]:
            raise ValueError("Step-mode threshold temperature cannot exceed the CK-1 temperature target / guide.")
        if normalized["HEATING_TRIGGER_TEMP_C"] > normalized["RATE_CONTROL_MAX_TEMP_C"]:
            raise ValueError("The rate-control temperature ceiling cannot be below the CK-1 temperature target / guide.")
        if normalized["TEMP_WATCHDOG_MAX_TEMP_C"] <= normalized["RATE_CONTROL_MAX_TEMP_C"]:
            raise ValueError("The watchdog maximum temperature must be above the rate-control temperature ceiling.")
        if normalized["RATE_PID_MIN_CONTROL_TEMP_C"] > normalized["RATE_CONTROL_MAX_TEMP_C"]:
            raise ValueError("The minimum temperature for rate PID cannot exceed the rate-control temperature ceiling.")
        if normalized["RATE_PID_ACTIVATION_A_PER_S"] >= normalized["CK1_RATE_TARGET_A_PER_S"]:
            raise ValueError("The rate PID activation threshold must be below the CK-1 rate target.")
        if normalized["RATE_PID_DEADBAND_A_PER_S"] >= normalized["CK1_RATE_TARGET_A_PER_S"]:
            raise ValueError("The rate PID dead band must be smaller than the CK-1 rate target.")
        if normalized["RATE_ESTIMATOR_MIN_SPAN_S"] > normalized["RATE_ESTIMATOR_WINDOW_S"]:
            raise ValueError("The minimum rate-estimator time span cannot exceed its window.")
        if normalized["FRESH_PROFILE_MAX_TARGET_STEP_C"] > normalized["CASCADE_MAX_TARGET_STEP_C"]:
            raise ValueError("Fresh-profile target action cannot exceed the general cascade target action cap.")
        if normalized["TEMP_SLOPE_MIN_SPAN_S"] > normalized["TEMP_SLOPE_WINDOW_S"]:
            raise ValueError("The minimum temperature-slope time span cannot exceed its window.")
        if normalized["PID_INTEGRAL_ACTIVE_ERROR_C"] <= normalized["PID_TEMP_BAND_C"]:
            raise ValueError("The PID integral activation range must be wider than the PID dead band.")
        if normalized["FAST_RATE_EXCURSION_CURRENT_STEP_A"] > normalized["RATE_PID_MAX_DOWN_STEP_A"]:
            raise ValueError("The fast rate-guard reduction cannot exceed the direct-rate maximum decrease.")
        if normalized["COMPOUND_TEMP_GUARD_BAND_C"] > (
            normalized["RATE_CONTROL_MAX_TEMP_C"] - normalized["RATE_PID_MIN_CONTROL_TEMP_C"]
        ):
            raise ValueError("The compound temperature guard band is wider than the available rate-control temperature range.")
        if not (
            normalized["TEMP_SLOPE_TARGET_EARLY_C_PER_MIN"]
            >= normalized["TEMP_SLOPE_TARGET_MID_C_PER_MIN"]
            >= normalized["TEMP_SLOPE_TARGET_LATE_C_PER_MIN"]
        ):
            raise ValueError("Slope targets must be ordered early ≥ middle ≥ late.")
    elif phase == "sputter":
        if normalized["target_ar_pressure_mbar"] > normalized["pressure_warning_mbar"]:
            raise ValueError("The Ar target pressure cannot be above the pressure warning threshold.")
        if normalized["pressure_warning_mbar"] >= normalized["pressure_emergency_mbar"]:
            raise ValueError("The emergency pressure limit must be greater than the normal pressure threshold.")
        if normalized["anneal_reset_c"] > normalized["anneal_target_c"]:
            raise ValueError("The PID reset temperature cannot exceed the annealing target.")
        if normalized["coscon_energy_tolerance_v"] >= normalized["coscon_energy_v"]:
            raise ValueError("The COSCON energy tolerance must be smaller than the energy target.")
        if normalized["coscon_emission_tolerance_a"] >= normalized["coscon_emission_a"]:
            raise ValueError("The COSCON emission tolerance must be smaller than the emission target.")
    elif phase == "anneal":
        if normalized["INITIAL_WAIT_TARGET_C"] > normalized["FIRST_STAGE_TARGET_C"]:
            raise ValueError("The initial wait target cannot exceed the first-stage target.")
        if normalized["FIRST_STAGE_TARGET_C"] > normalized["SECOND_STAGE_TARGET_C"]:
            raise ValueError("The first-stage target cannot exceed the second-stage target.")
    return normalized


def non_default_overrides(phase: str, values: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_phase_values(phase, values)
    defaults = defaults_for_phase(phase)
    return {key: value for key, value in normalized.items() if value != defaults[key]}


def encode_overrides(phase: str, values: Mapping[str, Any]) -> str:
    overrides = non_default_overrides(phase, values)
    payload = {"phase": phase, "parameters": overrides}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def load_phase_overrides(phase: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    raw = str(env.get(AUTOMATION_PARAMETERS_ENV, "")).strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid {AUTOMATION_PARAMETERS_ENV}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid {AUTOMATION_PARAMETERS_ENV}: expected a JSON object")
    payload_phase = payload.get("phase")
    if payload_phase != phase:
        raise RuntimeError(
            f"Automation parameters were prepared for phase {payload_phase!r}, not {phase!r}."
        )
    parameters = payload.get("parameters", {})
    if not isinstance(parameters, dict):
        raise RuntimeError("Automation parameter payload must contain a parameters object.")
    full = validate_phase_values(phase, parameters)
    defaults = defaults_for_phase(phase)
    return {key: full[key] for key in parameters if full[key] != defaults[key]}


def effective_values(phase: str, overrides: Mapping[str, Any]) -> dict[str, Any]:
    values = defaults_for_phase(phase)
    values.update(validate_phase_values(phase, overrides))
    return values


def format_override_summary(phase: str, overrides: Mapping[str, Any]) -> str:
    if not overrides:
        return "Run-only automation parameters: packaged defaults are active."
    by_key = {spec.key: spec for spec in specs_for_phase(phase)}
    lines = ["Run-only automation parameter overrides received from the launcher:"]
    for key, value in overrides.items():
        spec = by_key[key]
        display = spec.format_display(value)
        suffix = f" {spec.unit}" if spec.unit else ""
        lines.append(f"  - {spec.label}: {display}{suffix} ({key})")
    lines.append("These values apply only to this process and do not modify the source files.")
    return "\n".join(lines)


def write_effective_parameters(
    destination: str | os.PathLike[str],
    phase: str,
    overrides: Mapping[str, Any],
) -> Path:
    """Save the complete effective recipe and the explicit run-only overrides."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    by_key = {spec.key: spec for spec in specs_for_phase(phase)}
    effective = defaults_for_phase(phase)
    effective.update(overrides)
    payload = {
        "phase": phase,
        "source_files_modified": False,
        "automation_mode": os.environ.get(AUTOMATION_MODE_NAME_ENV, "Custom run settings"),
        "scope": "Startup values passed by the unified launcher; live in-phase GUI edits may change runtime values later.",
        "overrides": dict(overrides),
        "effective_parameters": effective,
        "display_parameters": {
            key: {
                "label": by_key[key].label,
                "value": by_key[key].display_value(value),
                "unit": by_key[key].unit,
            }
            for key, value in effective.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def apply_overrides_to_namespace(
    phase: str,
    namespace: MutableMapping[str, Any],
    overrides: Mapping[str, Any],
) -> None:
    """Assign validated run-only values to matching module constants."""

    validated = validate_phase_values(phase, overrides)
    for key in overrides:
        namespace[key] = validated[key]


def apply_overrides_to_object(phase: str, obj: Any, overrides: Mapping[str, Any]) -> None:
    """Assign validated run-only values to matching object/dataclass fields."""

    validated = validate_phase_values(phase, overrides)
    for key in overrides:
        if not hasattr(obj, key):
            raise AttributeError(f"{type(obj).__name__} has no automation parameter {key!r}")
        setattr(obj, key, validated[key])
