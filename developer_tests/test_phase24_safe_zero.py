from __future__ import annotations

from pathlib import Path

from npg_chamber.config.run_parameters import all_default_values, specs_for_phase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_phase2_abort_pid_default_is_zero() -> None:
    defaults = all_default_values()["sputter"]
    assert defaults["abort_reset_c"] == 0.0

    phase2 = (PROJECT_ROOT / "npg_chamber/legacy_scripts/02_sputtering_annealing_legacy.py").read_text(
        encoding="utf-8"
    )
    assert "abort_reset_c: float = 0.0" in phase2
    assert "abort_reset_c: float = 20.0" not in phase2


def test_phase4_finishes_after_ten_minutes_at_cooldown_target() -> None:
    defaults = all_default_values()["anneal"]
    assert defaults["COOLDOWN_TARGET_C"] == 0.0
    assert defaults["POST_COOLDOWN_WAIT_S"] == 10 * 60
    assert "FINAL_VENT_TARGET_C" not in {spec.key for spec in specs_for_phase("anneal")}

    phase4 = (PROJECT_ROOT / "npg_chamber/legacy_scripts/04_npg_annealings_legacy.py").read_text(
        encoding="utf-8"
    )
    assert "pid.set_setpoint_c(COOLDOWN_TARGET_C)" in phase4
    assert "hold_for_seconds(state, POST_COOLDOWN_WAIT_S, final_hold_message)" in phase4
    assert "Oven PID setpoint remains at" in phase4
    assert "FINAL_VENT_TARGET_C" not in phase4
    assert "FINAL_SETPOINT_30" not in phase4
    assert "30 °C" not in phase4
    assert 'if self.state.finished_event.is_set() and self.state.phase == "FINISHED":' in phase4
    assert 'and self.state.evaporator_poweroff_confirmed_event.is_set()' not in phase4
    assert 'Finalization safety check: Keysight output confirmed OFF.' in phase4


def test_phase4_abort_uses_cooldown_target_and_sequence_is_lower() -> None:
    phase4 = (PROJECT_ROOT / "npg_chamber/legacy_scripts/04_npg_annealings_legacy.py").read_text(
        encoding="utf-8"
    )
    assert "self.pid.set_setpoint_c_best_effort(COOLDOWN_TARGET_C)" in phase4
    assert 'panel_text(0.05, 0.145, "Phase sequence"' in phase4
    assert '0.05, 0.125, "", transform=panel_ax.transAxes' in phase4
