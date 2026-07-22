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


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    label: str
    default: Any
    kind: str = "float"  # float | int | bool | choice
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
        if self.kind == "choice":
            return str(value)
        if self.kind == "int":
            return str(int(round(float(value))))
        return f"{float(value):.12g}"


# Safety hard stops, COM ports, baud rates, plotting, logging and GUI settings
# are intentionally excluded.  This editor is only for the experimental recipe
# and automation behaviour that operators currently change in source code.
def _ck1_common(*, include_calibration_target: bool) -> tuple[ParameterSpec, ...]:
    specs: list[ParameterSpec] = [
        ParameterSpec("HEATING_TRIGGER_TEMP_C", "CK-1 ready temperature", 242.0, unit="°C", group="Process targets", description="Temperature required before the shutter-open prompt.", minimum=0, maximum=450),
        ParameterSpec("CK1_RATE_TARGET_A_PER_S", "CK-1 rate target", 0.40, unit="Å/s", group="Process targets", description="Average QMB rate required before the shutter-open prompt.", minimum=0.001, maximum=5),
        ParameterSpec("CK1_RATE_AVG_WINDOW_POINTS", "Rate averaging points", 8, kind="int", group="Process targets", description="Number of recent CK-1 rate readings used for the readiness average.", minimum=1, maximum=200),
        ParameterSpec("KEYSIGHT_START_CURRENT_A", "Starting current", 0.005, unit="A", group="Keysight ramp-up", description="Initial non-zero current commanded when automatic heating starts.", minimum=0, maximum=0.670),
        ParameterSpec("KEYSIGHT_BASE_WORK_CURRENT_A", "Base working current", 0.640, unit="A", group="Keysight ramp-up", description="Working-current reference used by the ramp controller.", minimum=0.001, maximum=0.670),
        ParameterSpec("KEYSIGHT_STEP_A", "Current step", 0.005, unit="A", group="Keysight ramp-up", description="Fixed current increment used by step-based ramping.", minimum=0.0001, maximum=0.05),
        ParameterSpec("KEYSIGHT_STEP_PERIOD_S", "General step period", 15.0, unit="s", group="Keysight ramp-up", description="Default delay between automatic current steps.", minimum=0.1, maximum=600),
        ParameterSpec("DEFAULT_RAMP_UP_MODE", "Default ramp-up mode", "steps", kind="choice", choices=("steps", "slope"), group="Keysight ramp-up", description="Ramp strategy selected when the phase starts."),
        ParameterSpec("STEPS_RAMP_UNTIL_TEMP_C", "Step mode until temperature", 100.0, unit="°C", group="Keysight ramp-up", description="Temperature threshold used by the step-ramp approach.", minimum=0, maximum=450),
        ParameterSpec("STEPS_RAMP_STEP_PERIOD_S", "Step mode period", 15.0, unit="s", group="Keysight ramp-up", description="Delay between current increments in step mode.", minimum=0.1, maximum=600),
        ParameterSpec("PID_CONTROL_PERIOD_S", "PID control period", 8.0, unit="s", group="Temperature PID", description="Time between CK-1 temperature PID corrections.", minimum=0.1, maximum=120),
        ParameterSpec("PID_TEMP_BAND_C", "PID temperature band", 1.0, unit="°C", group="Temperature PID", description="Dead band around the CK-1 target temperature.", minimum=0, maximum=50),
        ParameterSpec("PID_KP_A_PER_C", "PID Kp", 0.0020, unit="A/°C", group="Temperature PID", description="Proportional gain for CK-1 temperature control.", minimum=0, maximum=0.1),
        ParameterSpec("PID_KI_A_PER_C_S", "PID Ki", 0.000030, unit="A/(°C·s)", group="Temperature PID", description="Integral gain for CK-1 temperature control.", minimum=0, maximum=0.01),
        ParameterSpec("PID_KD_A_PER_C_PER_S", "PID Kd", 0.0030, unit="A/(°C/s)", group="Temperature PID", description="Derivative gain for CK-1 temperature control.", minimum=0, maximum=1),
        ParameterSpec("PID_MAX_STEP_A", "PID maximum correction", 0.0025, unit="A", group="Temperature PID", description="Maximum current change allowed in one PID update.", minimum=0.00001, maximum=0.05),
        ParameterSpec("PID_INTEGRAL_LIMIT_C_S", "PID integral limit", 250.0, unit="°C·s", group="Temperature PID", description="Absolute anti-windup limit for accumulated PID error.", minimum=0, maximum=100000),
        ParameterSpec("TEMP_SLOPE_WINDOW_POINTS", "Slope averaging points", 15, kind="int", group="Slope ramp", description="Number of CK-1 temperature readings used to estimate slope.", minimum=2, maximum=500),
        ParameterSpec("TEMP_SLOPE_TARGET_EARLY_C_PER_MIN", "Early slope target", 9.0, unit="°C/min", group="Slope ramp", description="Target heating slope in the early-current region.", minimum=0, maximum=100),
        ParameterSpec("TEMP_SLOPE_TARGET_MID_C_PER_MIN", "Middle slope target", 8.0, unit="°C/min", group="Slope ramp", description="Target heating slope in the middle-current region.", minimum=0, maximum=100),
        ParameterSpec("TEMP_SLOPE_TARGET_LATE_C_PER_MIN", "Late slope target", 7.0, unit="°C/min", group="Slope ramp", description="Target heating slope in the late-current region.", minimum=0, maximum=100),
        ParameterSpec("TEMP_SLOPE_DEADBAND_C_PER_MIN", "Slope dead band", 0.20, unit="°C/min", group="Slope ramp", description="Slope error band in which no correction is made.", minimum=0, maximum=20),
        ParameterSpec("TEMP_SLOPE_KP_A_PER_C_PER_MIN", "Slope controller Kp", 0.010, unit="A/(°C/min)", group="Slope ramp", description="Current correction gain for slope-based ramping.", minimum=0, maximum=1),
        ParameterSpec("FAST_RAMP_CURRENT_THRESHOLD_A", "Early/middle current boundary", 0.50, unit="A", group="Slope ramp", description="Current at which the slope controller changes from early to middle settings.", minimum=0, maximum=0.670),
        ParameterSpec("MID_RAMP_CURRENT_THRESHOLD_A", "Middle/late current boundary", 0.60, unit="A", group="Slope ramp", description="Current at which the slope controller changes from middle to late settings.", minimum=0, maximum=0.670),
        ParameterSpec("EARLY_RAMP_MAX_STEP_A", "Early maximum step", 0.005, unit="A", group="Slope ramp", description="Maximum slope-controller current correction in the early region.", minimum=0.00001, maximum=0.05),
        ParameterSpec("MID_RAMP_MAX_STEP_A", "Middle maximum step", 0.005, unit="A", group="Slope ramp", description="Maximum slope-controller current correction in the middle region.", minimum=0.00001, maximum=0.05),
        ParameterSpec("LATE_RAMP_MAX_STEP_A", "Late maximum step", 0.005, unit="A", group="Slope ramp", description="Maximum slope-controller current correction in the late region.", minimum=0.00001, maximum=0.05),
        ParameterSpec("RAMPDOWN_STEP_A", "Ramp-down current step", 0.010, unit="A", group="Ramp-down", description="Current reduction per safe ramp-down action.", minimum=0.0001, maximum=0.1),
        ParameterSpec("RAMPDOWN_STEP_PERIOD_S", "Ramp-down step period", 15, kind="int", unit="s", group="Ramp-down", description="Delay between current reductions during ramp-down.", minimum=1, maximum=3600),
        ParameterSpec("RAMPDOWN_ZERO_THRESHOLD_A", "Ramp-down zero threshold", 0.003, unit="A", group="Ramp-down", description="Current below which the output is treated as effectively zero.", minimum=0, maximum=0.05),
    ]
    if include_calibration_target:
        specs.insert(3, ParameterSpec("CALIBRATION_TARGET_SAMPLE_A", "Sample calibration target", 1.0, unit="Å", group="Process targets", description="Sample-relative thickness that ends Phase 01 calibration.", minimum=0.001, maximum=100))
    return tuple(specs)


PHASE_PARAMETER_SPECS: dict[str, tuple[ParameterSpec, ...]] = {
    "heat": _ck1_common(include_calibration_target=True),
    "sputter": (
        ParameterSpec("cycles", "Number of cycles", 3, kind="int", group="Workflow", description="Number of sputtering-annealing cycles.", minimum=1, maximum=100),
        ParameterSpec("expected_degassing_minutes", "Degassing guide time", 20.0, unit="min", group="Workflow", description="Guide countdown for initial degassing in cycle 1.", minimum=0, maximum=1440),
        ParameterSpec("sputter_minutes", "Sputtering duration", 20.0, unit="min", group="Workflow", description="Countdown duration for each sputtering step.", minimum=0, maximum=1440),
        ParameterSpec("anneal_target_c", "Annealing target", 620.0, unit="°C", group="Annealing", description="Oven PID setpoint used for each annealing stage.", minimum=0, maximum=750),
        ParameterSpec("anneal_hold_minutes", "Annealing hold", 10.0, unit="min", group="Annealing", description="Hold duration after the oven reaches the target window.", minimum=0, maximum=1440),
        ParameterSpec("anneal_reset_c", "PID reset after cycle", 0.0, unit="°C", group="Annealing", description="PID setpoint written after each annealing hold.", minimum=0, maximum=750),
        ParameterSpec("abort_reset_c", "PID reset on abort", 20.0, unit="°C", group="Annealing", description="PID setpoint attempted during an abort.", minimum=0, maximum=750),
        ParameterSpec("target_ar_pressure_mbar", "Target Ar pressure", 2.0e-5, unit="mbar", group="Pressure guidance", description="Operator guidance target for argon pressure.", minimum=1e-12, maximum=1),
        ParameterSpec("pressure_warning_mbar", "Pressure warning threshold", 5.0e-5, unit="mbar", group="Pressure guidance", description="Pressure above which the interface shows a warning.", minimum=1e-12, maximum=1),
        ParameterSpec("temperature_reach_tolerance_c", "Target reach tolerance", 5.0, unit="°C", group="Temperature detection", description="The oven is considered near target at target minus this tolerance.", minimum=0, maximum=100),
        ParameterSpec("stable_temperature_reads", "Stable readings required", 3, kind="int", group="Temperature detection", description="Consecutive near-target readings required before the hold starts.", minimum=1, maximum=1000),
        ParameterSpec("try_reset_pid_on_abort", "Reset PID on abort", True, kind="bool", group="Abort behaviour", description="Attempt to write the abort reset setpoint during abort."),
    ),
    "dpdbba": (
        ParameterSpec("DP_DBBA_SAMPLE_EQUIVALENT_THICKNESS_A", "DP-DBBA sample-equivalent target", 623.13 / 94.39, unit="Å", group="Process targets", description="Sample-equivalent amount multiplied by the Phase 01 ratio to obtain the CK-1 target.", minimum=0.001, maximum=1000),
        ParameterSpec("OVEN_TARGET_TEMPERATURE_C", "Oven PID target at startup", 200.0, unit="°C", group="Process targets", description="External oven PID setpoint written when Phase 03 starts.", minimum=0, maximum=750),
        *_ck1_common(include_calibration_target=False),
    ),
    "anneal": (
        ParameterSpec("INITIAL_WAIT_S", "Initial wait", 5 * 60, unit="min", group="Initial stage", description="Time held at the initial oven target.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("INITIAL_WAIT_TARGET_C", "Initial wait target", 200.0, unit="°C", group="Initial stage", description="PID setpoint during the initial wait.", minimum=0, maximum=750),
        ParameterSpec("FIRST_STAGE_TARGET_C", "First-stage target", 350.0, unit="°C", group="Annealing recipe", description="First NPG annealing temperature.", minimum=0, maximum=750),
        ParameterSpec("FIRST_STAGE_HOLD_S", "First-stage hold", 15 * 60, unit="min", group="Annealing recipe", description="Hold duration at the first-stage temperature.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("SECOND_STAGE_TARGET_C", "Second-stage target", 600.0, unit="°C", group="Annealing recipe", description="Second NPG annealing temperature.", minimum=0, maximum=750),
        ParameterSpec("SECOND_STAGE_HOLD_S", "Second-stage hold", 40 * 60, unit="min", group="Annealing recipe", description="Hold duration at the second-stage temperature.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("STAGE_REACHED_MARGIN_C", "Stage reach margin", 1.0, unit="°C", group="Annealing recipe", description="A ramp is complete when PV is at least target minus this margin.", minimum=0, maximum=100),
        ParameterSpec("COOLDOWN_TARGET_C", "Cooldown target", 0.0, unit="°C", group="Finalization", description="PID setpoint sent after the second hold.", minimum=0, maximum=750),
        ParameterSpec("POST_COOLDOWN_WAIT_S", "Post-cooldown wait", 10 * 60, unit="min", group="Finalization", description="Wait after writing the cooldown target.", minimum=0, maximum=7 * 24 * 3600, display_scale=60),
        ParameterSpec("FINAL_VENT_TARGET_C", "Final PID target", 30.0, unit="°C", group="Finalization", description="Final PID setpoint written before normal completion.", minimum=0, maximum=750),
        ParameterSpec("KEYSIGHT_RAMPDOWN_STEP_A", "Keysight ramp-down step", 0.005, unit="A", group="Keysight ramp-down", description="Current reduction per ramp-down step.", minimum=0.0001, maximum=0.1),
        ParameterSpec("KEYSIGHT_RAMPDOWN_STEP_S", "Keysight step period", 15, kind="int", unit="s", group="Keysight ramp-down", description="Delay between current reductions.", minimum=1, maximum=3600),
        ParameterSpec("FIRST_RAMPDOWN_STEP_DELAY_S", "First ramp-down delay", 10, kind="int", unit="s", group="Keysight ramp-down", description="Delay before the first current reduction.", minimum=0, maximum=3600),
        ParameterSpec("KEYSIGHT_ZERO_THRESHOLD_A", "Current zero threshold", 0.003, unit="A", group="Keysight ramp-down", description="Current below which the output is treated as zero.", minimum=0, maximum=0.05),
    ),
}


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
        if normalized["FAST_RAMP_CURRENT_THRESHOLD_A"] >= normalized["MID_RAMP_CURRENT_THRESHOLD_A"]:
            raise ValueError("The early/middle current boundary must be below the middle/late boundary.")
        if normalized["MID_RAMP_CURRENT_THRESHOLD_A"] > normalized["KEYSIGHT_BASE_WORK_CURRENT_A"]:
            raise ValueError("The middle/late current boundary cannot exceed the base working current.")
        if normalized["STEPS_RAMP_UNTIL_TEMP_C"] > normalized["HEATING_TRIGGER_TEMP_C"]:
            raise ValueError("Step-mode threshold temperature cannot exceed the CK-1 ready temperature.")
        if not (
            normalized["TEMP_SLOPE_TARGET_EARLY_C_PER_MIN"]
            >= normalized["TEMP_SLOPE_TARGET_MID_C_PER_MIN"]
            >= normalized["TEMP_SLOPE_TARGET_LATE_C_PER_MIN"]
        ):
            raise ValueError("Slope targets must be ordered early ≥ middle ≥ late.")
    elif phase == "sputter":
        if normalized["target_ar_pressure_mbar"] > normalized["pressure_warning_mbar"]:
            raise ValueError("The Ar target pressure cannot be above the pressure warning threshold.")
        if normalized["anneal_reset_c"] > normalized["anneal_target_c"]:
            raise ValueError("The PID reset temperature cannot exceed the annealing target.")
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
