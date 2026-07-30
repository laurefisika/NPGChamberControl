from __future__ import annotations

from pathlib import Path

from npg_chamber.config.run_parameters import defaults_for_phase, specs_for_phase


ROOT = Path(__file__).resolve().parents[1]
PHASES = (
    ROOT / "npg_chamber" / "legacy_scripts" / "01_heat_up_calibration_legacy.py",
    ROOT / "npg_chamber" / "legacy_scripts" / "03_dp_dbba_evaporation_legacy.py",
)


def test_phase_01_and_03_offer_the_same_three_feedback_modes() -> None:
    for phase in ("heat", "dpdbba"):
        specs = {spec.key: spec for spec in specs_for_phase(phase)}
        mode = specs["EVAPORATION_CONTROL_MODE"]
        assert mode.choices == ("temperature", "rate", "compound")
        # Existing validated behavior remains the packaged default until the
        # new feedback loop has been tested on the real chamber.
        assert defaults_for_phase(phase)["EVAPORATION_CONTROL_MODE"] == "temperature"


def test_phase_01_and_03_integrate_rate_feedback_with_temperature_supervision() -> None:
    required = (
        "RatePidController(",
        "def filtered_ck1_rate(",
        "def apply_rate_pid_control(",
        "def check_active_rate_signal_or_stop(",
        "RATE_PID_SIGNAL_TIMEOUT_S",
        "RATE_CONTROL_MAX_TEMP_C",
        "COMPOUND_TEMP_GUARD_BAND_C",
        "compound_temperature_guard=True",
        "get_temperature_watchdog_reference_c()",
        "RATE FEEDBACK HARD STOP",
        "Feedback mode:",
        "Active control:",
    )
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        for token in required:
            assert token in source


def test_rate_feedback_stays_active_during_open_shutter_deposition() -> None:
    phase_01 = PHASES[0].read_text(encoding="utf-8")
    phase_03 = PHASES[1].read_text(encoding="utf-8")
    assert "('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'CALIBRATION', 'WAIT_SHUTTER_CLOSE')" in phase_01
    assert "('HEATING_UP', 'WAIT_SHUTTER_OPEN', 'EVAPORATION', 'WAIT_SHUTTER_CLOSE')" in phase_03


def test_phase_03_preserves_rate_history_when_shutter_opens() -> None:
    source = PHASES[1].read_text(encoding="utf-8")
    start = source.index("def reset_evaporation_measurement_window()")
    end = source.index("\ndef request_snapshot", start)
    reset_function = source[start:end]
    assert "['thickness_times'].clear()" in reset_function
    assert "['thickness_data'].clear()" in reset_function
    assert "['rate_times'].clear()" not in reset_function
    assert "['rate_data'].clear()" not in reset_function


def test_rate_pid_evaluation_respects_configured_control_period() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        start = source.index("def apply_rate_pid_control(")
        end = source.index("\ndef reset_temperature_pid", start)
        function = source[start:end]
        assert "keysight_state['last_step_at'] = now" in function


def test_rate_pid_handover_requires_a_fresh_qmb_rate() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        start = source.index("def rate_feedback_can_control(")
        end = source.index("\ndef check_active_rate_signal_or_stop", start)
        function = source[start:end]
        assert "latest_ck1_rate_age_s()" in function
        assert "rate_age_s > RATE_PID_SIGNAL_TIMEOUT_S" in function
