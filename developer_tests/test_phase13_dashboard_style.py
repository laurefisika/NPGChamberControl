from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PHASE_01 = ROOT / "npg_chamber" / "legacy_scripts" / "01_heat_up_calibration_legacy.py"
PHASE_03 = ROOT / "npg_chamber" / "legacy_scripts" / "03_dp_dbba_evaporation_legacy.py"
STYLE_HELPER = ROOT / "npg_chamber" / "common" / "phase_dashboard_style.py"


COMMON_PANEL_LABELS = (
    "Live controls and run status",
    "Editable heating targets",
    "Ramp-up settings",
    "Manual current override",
    "Operator controls",
    "Process status",
    "Last action",
    "Apply targets",
    "Reset targets",
    "Steps mode",
    "Slope mode",
    "Set manual current",
    "Resume automatic",
    "Open shutter",
    "Close shutter",
    "Abort / safe stop",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_phase_01_and_03_use_the_same_dashboard_language():
    phase_01 = _source(PHASE_01)
    phase_03 = _source(PHASE_03)
    for label in COMMON_PANEL_LABELS:
        assert label in phase_01
        assert label in phase_03


def test_phase_01_and_03_reserve_vertical_space_for_titles_and_selector():
    for source in (_source(PHASE_01), _source(PHASE_03)):
        assert "hspace=0.68" in source
        assert "top=0.875" in source
        assert "right=0.685" in source
        assert "panel_left = 0.705" in source
        assert "panel_bottom = 0.012" in source
        assert "panel_width = 0.282" in source
        assert "panel_height = 0.976" in source
        assert "graph_header_height = selector_height + selector_gap" in source
        assert "selector_lift = 0.052" in source
        assert "y = bbox.y1 + selector_lift" in source
        assert "pad=28" in source


def test_phase_01_and_03_keep_distinct_workflows_and_completion_controls():
    phase_01 = _source(PHASE_01)
    phase_03 = _source(PHASE_03)

    # The external oven PID startup target belongs to DP-DBBA, never Phase 01.
    assert "OVEN_TARGET_TEMPERATURE_C" not in phase_01
    assert "set_oven_pid_setpoint" not in phase_01
    assert 'OVEN_TARGET_TEMPERATURE_C = RUN_AUTOMATION_OVERRIDES.get("OVEN_TARGET_TEMPERATURE_C", 200.0)' in phase_03
    assert "set_oven_pid_setpoint(OVEN_TARGET_TEMPERATURE_C)" in phase_03

    # Both dashboards expose Finish phase, but Phase 03 only accepts it at its
    # established WAIT_SHUTTER_CLOSE normal-handoff point.
    assert "Finish phase" in phase_01
    assert "Finish phase" in phase_03
    assert "if phase != 'WAIT_SHUTTER_CLOSE':" in phase_03
    assert "leave the Keysight ON at base current for Phase 04" in phase_03

    # Phase 03 GUI abort lowers the oven PID target before the independent
    # Keysight ramp-down/OFF sequence. Normal Finish phase does not do this.
    abort_start = phase_03.index("def request_gui_abort")
    finish_start = phase_03.index("def request_gui_finish")
    abort_source = phase_03[abort_start:finish_start]
    assert "set_oven_pid_setpoint(0.0)" in abort_source
    assert abort_source.index("set_oven_pid_setpoint(0.0)") < abort_source.index("rampdown_keysight_output")
    finish_source = phase_03[finish_start:phase_03.index("def _gui_open_shutter", finish_start)]
    assert "set_oven_pid_setpoint(0.0)" not in finish_source


def test_phase_01_and_03_use_shared_presentation_only_style_helper():
    helper = _source(STYLE_HELPER)
    assert "presentation-only helpers" in helper
    assert "style_measurement_axis" in helper
    assert "add_panel_card" in helper
    assert "create_phase_badge" in helper
    for source in (_source(PHASE_01), _source(PHASE_03)):
        assert "from npg_chamber.common.phase_dashboard_style import" in source
        assert "update_phase_badge(phase_title_text" in source


def test_redundant_pyrometer_emissivity_launcher_is_removed():
    assert not (ROOT / "CHECK_PYROMETER_EMISSIVITY.bat").exists()
    assert not (ROOT / "diagnostic_tools" / "check_pyrometer_emissivity.py").exists()
    assert "CHECK_PYROMETER_EMISSIVITY.bat" not in _source(ROOT / "MANIFEST.in")
