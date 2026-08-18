from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P1 = ROOT / "npg_chamber" / "legacy_scripts" / "01_heat_up_calibration_legacy.py"
P3 = ROOT / "npg_chamber" / "legacy_scripts" / "03_dp_dbba_evaporation_legacy.py"


def _block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


def test_phase01_abort_is_immediate_off_and_finish_is_controlled_rampdown() -> None:
    text = P1.read_text(encoding="utf-8")
    abort = _block(text, "def request_gui_abort", "def request_gui_finish")
    finish = _block(text, "def request_gui_finish", "def _gui_open_shutter")
    assert "emergency_keysight_shutdown('GUI Abort button - immediate phase stop')" in abort
    assert abort.index("emergency_keysight_shutdown") < abort.index("request_snapshot")
    assert "process_state['phase'] = 'RAMP_DOWN'" in finish
    assert "process_state['gui_auto_close'] = True" in finish


def test_phase03_abort_is_immediate_off_and_finish_hands_off_at_base_current() -> None:
    text = P3.read_text(encoding="utf-8")
    abort = _block(text, "def request_gui_abort", "def request_gui_finish")
    handoff = _block(text, "def hold_keysight_for_next_script", "def leave_keysight_on_message")
    assert "emergency_keysight_shutdown('GUI Abort button - immediate phase stop')" in abort
    assert abort.index("emergency_keysight_shutdown") < abort.index("set_oven_pid_setpoint(0.0)")
    assert "rampdown_keysight_output" not in text
    assert "target_current = clamp(KEYSIGHT_BASE_WORK_CURRENT_A" in handoff
    assert "keysight_write('OUTP ON')" in handoff
    assert "Set current has been returned to base current" in handoff


def test_phase03_no_longer_exposes_obsolete_abort_rampdown_parameters() -> None:
    from npg_chamber.config.run_parameters import specs_for_phase

    keys = {spec.key for spec in specs_for_phase("dpdbba")}
    assert "RAMPDOWN_STEP_A" not in keys
    assert "RAMPDOWN_STEP_PERIOD_S" not in keys
    assert "RAMPDOWN_ZERO_THRESHOLD_A" not in keys

    heat_keys = {spec.key for spec in specs_for_phase("heat")}
    assert "RAMPDOWN_STEP_A" in heat_keys
    assert "RAMPDOWN_STEP_PERIOD_S" in heat_keys
