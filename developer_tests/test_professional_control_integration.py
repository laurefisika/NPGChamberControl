from __future__ import annotations

from pathlib import Path

from npg_chamber.config.run_parameters import defaults_for_phase

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "npg_chamber/legacy_scripts/01_heat_up_calibration_legacy.py"
PHASE2 = ROOT / "npg_chamber/legacy_scripts/02_sputtering_annealing_legacy.py"
PHASE3 = ROOT / "npg_chamber/legacy_scripts/03_dp_dbba_evaporation_legacy.py"
PHASE4 = ROOT / "npg_chamber/legacy_scripts/04_npg_annealings_legacy.py"


def test_phase13_compound_is_true_cascade() -> None:
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        assert "CascadeRateController(" in source
        assert "def apply_compound_cascade_control(" in source
        assert "rate PI -> temperature target -> temperature PID -> current" in source
        assert "robust_rate_from_thickness(" in source
        assert "latest_control_ck1_temperature()" in source
        assert "last_valid_estimate_at" in source
        assert "effective_rate_settling_s()" in source


def test_phase3_requires_stable_external_oven_before_shutter() -> None:
    source = PHASE3.read_text(encoding="utf-8")
    assert "oven_ready_tracker = StableBandTracker" in source
    assert "if not oven_ready_for_evaporation():" in source
    defaults = defaults_for_phase("dpdbba")
    assert defaults["OVEN_READY_TOLERANCE_C"] == 2.0
    assert defaults["OVEN_READY_STABLE_DURATION_S"] == 60.0


def test_phase2_and_phase4_use_continuous_symmetric_stability_and_effective_holds() -> None:
    phase2 = PHASE2.read_text(encoding="utf-8")
    assert "temperature_stable_duration_s" in phase2
    assert "abs(float(temp) - float(target_c))" in phase2
    assert "pause_hold_outside_temperature_band" in phase2
    assert "effective_elapsed" in phase2

    phase4 = PHASE4.read_text(encoding="utf-8")
    assert "STAGE_STABLE_DURATION_S" in phase4
    assert "abs(float(current_temp) - target_c)" in phase4
    assert "PAUSE_HOLD_OUTSIDE_TEMPERATURE_BAND" in phase4
    assert "effective_elapsed_s" in phase4


def test_professional_profiles_do_not_change_fixed_safety_limits() -> None:
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        assert "KEYSIGHT_SOFT_WARNING_A = 0.660" in source
        assert "KEYSIGHT_HARD_STOP_A = 0.680" in source
        assert "TEMP_WATCHDOG_MAX_TEMP_C = 255.0" in source
        assert "KEYSIGHT_INSTRUMENT_OCP_A = KEYSIGHT_HARD_STOP_A + KEYSIGHT_INSTRUMENT_OCP_MARGIN_A" in source


def test_data_tuned_phase13_defaults_match_measured_dynamics() -> None:
    for phase in ("heat", "dpdbba"):
        defaults = defaults_for_phase(phase)
        assert defaults["RATE_ESTIMATOR_WINDOW_S"] == 60.0
        assert defaults["RATE_ESTIMATOR_MIN_SPAN_S"] == 45.0
        assert defaults["RATE_ESTIMATOR_MIN_R2"] == 0.80
        assert defaults["RATE_PID_CONTROL_PERIOD_S"] == 60.0
        assert defaults["RATE_CONTROL_SETTLING_S"] == 180.0
        assert defaults["FRESH_PROFILE_INITIAL_TARGET_OFFSET_C"] == -4.0
        assert defaults["FRESH_PROFILE_CASCADE_KI_SCALE"] == 0.0
        assert defaults["RATE_ESTIMATE_TREND_WINDOW_S"] == 180.0


def test_phase13_rate_trend_uses_robust_estimate_history() -> None:
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        assert "estimated_rate_times" in source
        assert "estimated_rate_data" in source
        assert "window_s=RATE_ESTIMATE_TREND_WINDOW_S" in source
        assert "initial_cascade_target_c(current_temp)" in source
        assert "Retarget without resetting" in source


def test_shutter_gate_is_decoupled_from_controller_internal_stability() -> None:
    obsolete = (
        "def readiness_quality_snapshot():",
        "READY_MAX_ABS_RATE_TREND_A_PER_S2",
        "READY_MIN_CURRENT_HEADROOM_A",
        "readiness_block_reason",
    )
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        for token in obsolete:
            assert token not in source
        assert "CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN" in source
        assert "effective_cascade_inner_temp_slope_limit_c_per_min()" in source

    for phase in ("heat", "dpdbba"):
        defaults = defaults_for_phase(phase)
        assert defaults["CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN"] == 0.30
        assert "RATE_PID_READY_STABLE_READS" not in defaults
        assert "RATE_READY_STABLE_DURATION_S" not in defaults
        assert "READY_MAX_ABS_TEMP_SLOPE_C_PER_MIN" not in defaults
        assert "READY_MAX_ABS_RATE_TREND_A_PER_S2" not in defaults
        assert "READY_MIN_CURRENT_HEADROOM_A" not in defaults
        assert defaults["CASCADE_MAX_TARGET_STEP_C"] == 1.0
        assert defaults["FRESH_PROFILE_MAX_TARGET_STEP_C"] == 0.75

def test_qmb_outlier_guard_precedes_control_and_is_audited() -> None:
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        assert "QmbSignalGuard(" in source
        assert "check_thickness(" in source
        assert "check_rate(" in source
        assert "record_qmb_rejection(" in source
        assert "_data_quality_events.csv" in source
        assert "data_quality_event_log:" in source


def test_phase1_calibration_uses_exact_crossing_and_quality_checks() -> None:
    source = PHASE1.read_text(encoding="utf-8")
    assert "def calculate_calibration_result():" in source
    assert "exact_calibration_ratio(" in source
    common = (ROOT / "npg_chamber/common/professional_control.py").read_text(encoding="utf-8")
    assert "crossing_time_s" in common
    assert "def exact_calibration_ratio(" in common
    assert "CALIBRATION_MIN_LINEAR_R2" in source
    defaults = defaults_for_phase("heat")
    assert defaults["CALIBRATION_MIN_LINEAR_R2"] == 0.985
    assert len([key for key in defaults if key.startswith("CALIBRATION_")]) == 2

def test_pre_refill_manual_runs_add_inner_loop_qualification_and_propagation_hold() -> None:
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        assert "def cascade_inner_loop_snapshot(" in source
        assert "cascade_inner_ready_tracker = StableConditionTracker" in source
        assert "CASCADE_INNER_READY_TEMP_BAND_C" in source
        assert "CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN" in source
        assert "CASCADE_THERMAL_RESPONSE_MAX_HOLD_S" in source
        assert "freeze=inner['freeze_outer']" in source
        assert "last_outer_delta_c" in source
        assert "last_outer_action_at" in source
        assert "Previous {last_delta:+.3f} ºC target action is still propagating" in source
        assert "Cascade inner ready:" in source
        assert "Thermal response pending:" in source

    for phase in ("heat", "dpdbba"):
        defaults = defaults_for_phase(phase)
        assert defaults["CASCADE_INNER_READY_TEMP_BAND_C"] == 0.75
        assert defaults["CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN"] == 0.30
        assert defaults["CASCADE_INNER_READY_STABLE_DURATION_S"] == 60.0
        assert defaults["CASCADE_THERMAL_RESPONSE_MAX_HOLD_S"] == 420.0

