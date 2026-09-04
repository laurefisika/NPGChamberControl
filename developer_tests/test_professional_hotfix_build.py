from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_build_identity_is_visible_and_source_guard_exists():
    init_text = (ROOT / "npg_chamber" / "__init__.py").read_text(encoding="utf-8")
    runner = (ROOT / "npg_chamber" / "workflows" / "runner.py").read_text(encoding="utf-8")
    launcher = (ROOT / "START_NPG_CHAMBER.bat").read_text(encoding="utf-8")
    assert '__build__ = "2026.09.04-r20"' in init_text
    assert "Software build: v{__version__} ({__build__})" in runner
    assert "Source build: %SOURCE_BUILD%" in launcher
    assert "goto source_mismatch" in launcher
    assert "npg_chamber.installation_check" in launcher
    verifier = (ROOT / "npg_chamber" / "installation_check.py").read_text(encoding="utf-8")
    assert "TEMP_SLOPE_WINDOW_S = 45.0" in verifier
    assert "apply_mode_button.clicked.connect(self._apply_feedback_mode)" in verifier
    assert "def _apply_adaptive_live_fit" in verifier
    assert "splitter.setSizes([1310, 540])" in verifier
    assert "DEFAULT_RAMP_UP_MODE" in verifier


def test_basic_mode_contains_operator_facing_pid_band_and_ramp_transition():
    text = (ROOT / "npg_chamber" / "gui_launcher.py").read_text(encoding="utf-8")
    # Both Phase 01 and Phase 03 sets should expose these two understandable run controls.
    assert text.count('"PID_TEMP_BAND_C"') >= 2
    assert text.count('"STEPS_RAMP_UNTIL_TEMP_C"') >= 2


def test_basic_mode_exposes_only_relevant_keysight_ramps() -> None:
    text = (ROOT / "npg_chamber" / "gui_launcher.py").read_text(encoding="utf-8")
    for key in (
        "DEFAULT_RAMP_UP_MODE",
        "KEYSIGHT_BASE_WORK_CURRENT_A",
        "KEYSIGHT_STEP_A",
        "STEPS_RAMP_STEP_PERIOD_S",
    ):
        assert text.count(f'"{key}"') >= 2

    # Phase 01 still has a normal Finish ramp-down. Phase 03 does not: its
    # Abort is immediate OFF and its normal Finish hands off at 0.640 A.
    assert text.count('"RAMPDOWN_STEP_A"') == 1
    assert text.count('"RAMPDOWN_STEP_PERIOD_S"') == 1

    for key in (
        "KEYSIGHT_RAMPDOWN_STEP_A",
        "KEYSIGHT_RAMPDOWN_STEP_S",
        "FIRST_RAMPDOWN_STEP_DELAY_S",
    ):
        assert f'"{key}"' in text


def test_phase13_operator_controls_precede_status_and_panel_is_wider() -> None:
    text = (ROOT / "npg_chamber" / "common" / "qt_phase_dashboard.py").read_text(encoding="utf-8")
    assert text.index("layout.addWidget(self.actions_section)") < text.index(
        "layout.addWidget(self.status_section)"
    )
    assert "splitter.setSizes([1310, 540])" in text
    assert "scroll.setMinimumWidth(440)" in text


def test_phase13_adaptive_live_fit_and_temperature_switch_rescale() -> None:
    text = (ROOT / "npg_chamber" / "common" / "qt_phase_dashboard.py").read_text(encoding="utf-8")
    assert "def _apply_adaptive_live_fit" in text
    assert "desired_span < 0.62 * old_span" in text
    assert "force_autoscale=temperature_view_changed" in text
    assert 'plot.setXRange(x_low, x_high, padding=0.015)' in text
    assert 'marker = self._shutter_open_timestamp' in text


def test_all_phase_guis_share_base_visual_vocabulary() -> None:
    qt = (ROOT / "npg_chamber" / "common" / "qt_phase_dashboard.py").read_text(encoding="utf-8")
    phase2 = (ROOT / "npg_chamber" / "phase_scripts" / "02_sputtering_annealing.py").read_text(encoding="utf-8")
    phase4 = (ROOT / "npg_chamber" / "phase_scripts" / "04_npg_annealings.py").read_text(encoding="utf-8")
    for token in ("#eef2f7", "#ffffff", "#dbe3ec", "#0f172a", "#64748b"):
        assert token in qt
        assert token in phase2
    assert '#eef2f7' in phase4
    assert '#c62828' in phase4
    assert '#1565c0' in phase4
    assert '#d4a000' in phase4
