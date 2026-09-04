from __future__ import annotations

import threading
import time
from pathlib import Path

from npg_chamber.common.pressure_alarm import PressureEmergencyAlarm


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_alarm_starts_above_threshold_and_clears_with_hysteresis():
    popup_calls = []
    beep_event = threading.Event()
    alarm = PressureEmergencyAlarm(
        threshold_mbar=5.0e-6,
        clear_threshold_mbar=4.5e-6,
        repeat_popup_s=60.0,
        beep_period_s=0.01,
        popup_function=lambda title, message: popup_calls.append((title, message)),
        beep_function=beep_event.set,
    )
    assert alarm.update(5.1e-6)
    assert wait_until(lambda: len(popup_calls) == 1)
    assert wait_until(beep_event.is_set)
    assert alarm.active
    assert alarm.update(4.8e-6)  # still active inside the hysteresis band
    assert not alarm.update(4.4e-6)
    alarm.close()


def test_invalid_readings_do_not_create_false_alarm():
    popup_calls = []
    alarm = PressureEmergencyAlarm(
        popup_function=lambda title, message: popup_calls.append((title, message)),
        beep_function=lambda: None,
    )
    assert not alarm.update(None)
    assert not alarm.update(float("nan"))
    assert not popup_calls
    alarm.close()


def test_phase_alarm_thresholds_match_their_operating_regimes():
    root = Path(__file__).resolve().parents[1]
    phase01 = (root / "npg_chamber/phase_scripts/01_heat_up_calibration.py").read_text(encoding="utf-8")
    phase02 = (root / "npg_chamber/phase_scripts/02_sputtering_annealing.py").read_text(encoding="utf-8")
    phase03 = (root / "npg_chamber/phase_scripts/03_dp_dbba_evaporation.py").read_text(encoding="utf-8")
    assert "PRESSURE_DESKTOP_ALARM_MBAR = 5.0e-6" in phase01
    assert "PRESSURE_DESKTOP_ALARM_MBAR = 5.0e-6" in phase03
    assert "threshold_mbar=self.cfg.pressure_emergency_mbar" in phase02
