from __future__ import annotations

from pathlib import Path


PHASES = [
    Path("npg_chamber/legacy_scripts/01_heat_up_calibration_legacy.py"),
    Path("npg_chamber/legacy_scripts/03_dp_dbba_evaporation_legacy.py"),
    Path("npg_chamber/legacy_scripts/04_npg_annealings_legacy.py"),
]


def test_phase1_phase3_and_phase4_have_monitoring_only_pyrometer_integration() -> None:
    for path in PHASES:
        text = path.read_text(encoding="utf-8")
        assert "ImpacIPE140" in text
        assert 'PyrometerSerialConfig(port="COM10", baudrate=38400, address="00")' in text
        assert "pyrometer" in text.lower()
        assert "sample" in text.lower()
        assert "emissivity" in text.lower()
        assert "monitor_pyrometer" in text or "read_pyrometer" in text
        assert "monitoring-only" in text.lower() or "monitoring only" in text.lower()


def test_all_pyrometer_phases_have_three_way_temperature_view() -> None:
    for path in PHASES:
        text = path.read_text(encoding="utf-8")
        assert "OVEN PID" in text
        assert "PYROMETER" in text
        assert "SAMPLE EST." in text


def test_below_cutoff_is_warned_but_not_discarded() -> None:
    for path in PHASES:
        text = path.read_text(encoding="utf-8")
        assert "extrapolated below calibrated range" in text
