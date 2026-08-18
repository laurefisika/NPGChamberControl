from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / "npg_chamber" / "legacy_scripts" / "01_heat_up_calibration_legacy.py"
PHASE3 = ROOT / "npg_chamber" / "legacy_scripts" / "03_dp_dbba_evaporation_legacy.py"


def _function_source(path: Path, name: str, next_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    start = source.index(f"def {name}(")
    end = source.index(f"\ndef {next_name}(", start)
    return source[start:end]


def test_phase13_shutter_readiness_is_minimal_and_operator_facing() -> None:
    for path in (PHASE1, PHASE3):
        function = _function_source(path, "heating_ready_for_shutter", "current_phase")
        assert "float(ck1_temp) >= float(target_temp)" in function
        assert "float(ck1_rate_avg) >= float(target_rate)" in function
        assert "readiness_quality_snapshot" not in function
        assert "rate_ready_since" not in function
        assert "ready_stable_reads" not in function
        assert "current_headroom" not in function
        assert "rate_trend" not in function
        assert "stable_time" not in function
        assert "get_ck1_rate_low_a_per_s" not in function
        assert "get_ck1_rate_high_a_per_s" not in function


def test_phase03_keeps_external_oven_stability_as_real_process_prerequisite() -> None:
    function = _function_source(PHASE3, "heating_ready_for_shutter", "current_phase")
    assert "if not oven_ready_for_evaporation():" in function
    assert "return False" in function


def test_obsolete_readiness_diagnostics_are_removed_from_phase13_runtime() -> None:
    obsolete = (
        "def readiness_quality_snapshot",
        "readiness_block_reason",
        "readiness_temp_slope_c_per_min",
        "readiness_rate_trend_a_per_s2",
        "readiness_current_headroom_a",
        "RATE_PID_READY_STABLE_READS",
        "RATE_READY_STABLE_DURATION_S",
        "READY_MAX_ABS_RATE_TREND_A_PER_S2",
        "READY_MIN_CURRENT_HEADROOM_A",
    )
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        for token in obsolete:
            assert token not in source
        assert "Control rate band:" in source


def test_transition_reason_does_not_depend_on_removed_readiness_diagnostics() -> None:
    for path in (PHASE1, PHASE3):
        source = path.read_text(encoding="utf-8")
        assert "Process targets reached: CK-1 T=" in source
        transition_start = source.index("if phase == 'HEATING_UP':")
        transition_end = source.index("elif phase == 'WAIT_SHUTTER_OPEN':", transition_start)
        transition = source[transition_start:transition_end]
        assert "readiness_" not in transition
