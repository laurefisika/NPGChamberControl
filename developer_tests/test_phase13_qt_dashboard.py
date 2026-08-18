from __future__ import annotations

from pathlib import Path
import threading

from npg_chamber.common.qt_phase_dashboard import (
    LatestFrameMailbox,
    PhaseDashboardSpec,
    TelemetryFrame,
    capture_telemetry_frame,
)


ROOT = Path(__file__).resolve().parents[1]
PHASES = (
    ROOT / "npg_chamber" / "legacy_scripts" / "01_heat_up_calibration_legacy.py",
    ROOT / "npg_chamber" / "legacy_scripts" / "03_dp_dbba_evaporation_legacy.py",
)
QT_DASHBOARD = ROOT / "npg_chamber" / "common" / "qt_phase_dashboard.py"


def test_qt_dashboard_has_isolated_professional_runtime() -> None:
    source = QT_DASHBOARD.read_text(encoding="utf-8")
    assert "class PhaseDashboardSpec" in source
    assert "class LatestFrameMailbox" in source
    assert "class TelemetryThread" in source
    assert "class CommandThread" in source
    assert "class CollapsibleSection" in source
    assert "queue.PriorityQueue" in source
    assert "Abort / safe stop" in source
    assert "priority=0" in source
    assert "No serial I/O, PID calculation" in source
    assert "Apply feedback controller" in source
    assert "Compound cascade" in source
    assert "Editable targets and controller" in source
    assert "curve.setDownsampling" in source
    assert "refresh_interval_ms: int = 250" in source
    assert "telemetry_interval_ms: int = 200" in source
    assert "max_plot_points: int = 400" in source
    assert "ThreadPoolExecutor" not in source
    for title in (
        "CK-1 QMB thickness",
        "CK-1 QMB rate",
        "Chamber pressure",
        "Sample QMB thickness",
        "Sample QMB rate",
        "Evaporator current",
        "Evaporator voltage",
        "CK-1 crucible temperature",
        "GUI runtime health",
    ):
        assert title in source


def test_latest_frame_mailbox_discards_obsolete_frames() -> None:
    mailbox = LatestFrameMailbox()
    assert mailbox.latest_after(0) is None

    first = TelemetryFrame(1, 1.0, 0.01, {"a": {}}, {"phase": "one"})
    third = TelemetryFrame(3, 3.0, 0.02, {"b": {}}, {"phase": "three"})
    mailbox.publish(first)
    mailbox.publish(third)

    latest = mailbox.latest_after(0)
    assert latest is third
    assert mailbox.latest_after(3) is None


def test_phase_01_and_03_default_to_qt_with_matplotlib_fallback() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        assert 'NPG_CHAMBER_PHASE13_GUI", "qt"' in source
        assert "USE_QT_PHASE13_DASHBOARD" in source
        assert 'matplotlib.use("Agg")' in source
        assert "build_qt_dashboard_spec" in source
        assert "run_phase_dashboard(build_qt_dashboard_spec())" in source
        assert "Fast Qt dashboard unavailable or disabled" in source
        assert "setup_live_target_controls()" in source
        assert "update_live_plot(snapshot)" in source


def test_qt_actions_reuse_established_phase_callbacks() -> None:
    phase_01 = PHASES[0].read_text(encoding="utf-8")
    phase_03 = PHASES[1].read_text(encoding="utf-8")
    for source in (phase_01, phase_03):
        assert "apply_manual_current_value" in source
        assert "confirm_shutter_open('PySide6 GUI button')" in source
        assert "confirm_shutter_closed('PySide6 GUI button')" in source
        assert "abort=request_gui_abort" in source
        assert "finish=request_gui_finish" in source
        assert "snapshot_provider=copy_plot_snapshot" in source
        assert "status_provider=qt_dashboard_status" in source
        assert "set_feedback_mode=qt_set_feedback_mode" in source
        assert "def set_evaporation_control_mode" in source
        assert "feedback_mode_action_lock" in source
        assert "run_feedback_mode_action" in source
        assert "Keysight current held at" in source
    assert "force_keysight_zero_output('Main cleanup before final plots')" in phase_01
    assert "normal_completion_leaves_keysight_on()" in phase_03


def test_phase_01_and_03_support_live_bumpless_feedback_mode_switching() -> None:
    dashboard = QT_DASHBOARD.read_text(encoding="utf-8")
    assert "set_feedback_mode: Callable[[str], None] | None = None" in dashboard
    assert "Temperature PID" in dashboard
    assert "Rate PID" in dashboard
    assert "Compound cascade" in dashboard
    assert "Controller + live targets requested" in dashboard

    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        assert "feedback_mode_lock = threading.Lock()" in source
        assert "feedback_mode_action_lock = threading.Lock()" in source
        assert "def get_evaporation_control_mode_snapshot" in source
        assert "def run_feedback_mode_action" in source
        assert "def set_evaporation_control_mode" in source
        assert "controller state reset bumplessly" in source
        assert "applied_delta=0.0" in source
        assert "set_feedback_mode=qt_set_feedback_mode" in source
        assert "'feedback_mode': get_evaporation_control_mode()" in source
        assert "'active_feedback_controller': get_active_feedback_controller()" in source


def test_project_declares_and_launcher_verifies_qt_dependencies() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    launcher = (ROOT / "START_NPG_CHAMBER.bat").read_text(encoding="utf-8")
    assert 'version = "0.9.36"' in pyproject
    assert '"PySide6-Essentials>=6.7,<7"' in pyproject
    assert '"pyqtgraph>=0.13.7,<0.15"' in pyproject
    assert '"numpy>=1.26"' in pyproject
    assert "'PySide6'" in launcher
    assert "'pyqtgraph'" in launcher
    assert "u.find_spec(m)" in launcher
    assert "import webview.platforms.winforms" not in launcher


def _no_op(*_args, **_kwargs) -> None:
    return None


def test_status_provider_failure_does_not_blank_live_plot_snapshot() -> None:
    snapshot = {
        "CK-1 evaporator QMB": {
            "rate_times": [1.0],
            "rate_data": [0.42],
        }
    }

    def broken_status():
        raise NameError("live_action_status_lock is not defined")

    spec = PhaseDashboardSpec(
        window_title="Test",
        phase_name="Test",
        snapshot_provider=lambda: snapshot,
        status_provider=broken_status,
        stop_event=threading.Event(),
        apply_targets=_no_op,
        reset_targets=_no_op,
        apply_ramp=_no_op,
        reset_ramp=_no_op,
        set_manual_current=_no_op,
        resume_automatic_current=_no_op,
        open_shutter=_no_op,
        close_shutter=_no_op,
        abort=_no_op,
        finish=_no_op,
        set_temperature_view=_no_op,
    )

    result = capture_telemetry_frame(spec, 1)
    assert result.frame.snapshot == snapshot
    assert result.frame.status["phase_label"] == "TELEMETRY DEGRADED"
    assert "Status provider failed" in result.errors[0]


def test_phase_01_and_03_define_shared_live_action_state() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        lock_pos = source.index("live_action_status_lock = threading.Lock()")
        text_pos = source.index("live_action_status_text = ''")
        provider_pos = source.index("def _qt_last_action_text")
        assert lock_pos < provider_pos
        assert text_pos < provider_pos


def test_qt_mode_skips_the_duplicate_matplotlib_live_dashboard() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        assert "does not need a second 3x3 Matplotlib live canvas" in source
        assert "fig = Figure(figsize=(1.0, 1.0))" in source
        assert "def update_live_plot(snapshot, force_autoscale=False):" in source
        assert "if USE_QT_PHASE13_DASHBOARD:" in source
        assert "global plot_refresh_counter" in source


def _load_feedback_mode_functions(path: Path):
    import ast
    import time

    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    wanted = {
        "get_evaporation_control_mode",
        "get_evaporation_control_mode_snapshot",
        "feedback_mode_is_current",
        "run_feedback_mode_action",
        "set_evaporation_control_mode",
    }
    functions = [
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    isolated = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(isolated)

    calls: dict[str, list] = {
        "rate_resets": [],
        "temperature_resets": [],
        "cascade_resets": [],
        "actions": [],
        "logs": [],
    }

    class _StopEvent:
        @staticmethod
        def is_set() -> bool:
            return False

    class _CascadeController:
        @staticmethod
        def reset(**kwargs) -> None:
            calls["cascade_resets"].append(kwargs)

    class _Color:
        CYAN = ""
        LIGHTBLACK_EX = ""
        RESET_ALL = ""

    namespace = {
        "threading": threading,
        "time": time,
        "CONTROL_MODE_TEMPERATURE": "temperature",
        "CONTROL_MODE_RATE": "rate",
        "CONTROL_MODE_COMPOUND": "compound",
        "EVAPORATION_CONTROL_MODE": "temperature",
        "feedback_mode_lock": threading.Lock(),
        "feedback_mode_action_lock": threading.Lock(),
        "feedback_mode_state": {
            "mode": "temperature",
            "generation": 0,
            "previous_mode": None,
            "changed_at": 0.0,
            "reason": "startup",
        },
        "stop_event": _StopEvent(),
        "keysight_state": {"set_current_a": 0.642, "last_step_at": 0.0},
        "latest_value": lambda *_args: 0.642,
        "latest_control_ck1_temperature": lambda: 242.0,
        "reset_rate_pid": lambda reason="": calls["rate_resets"].append(reason),
        "initial_cascade_target_c": lambda _temp=None: 238.0,
        "cascade_rate_controller": _CascadeController(),
        "get_heating_trigger_temp_c": lambda: 243.0,
        "feedback_control_state": {"active_temperature_target_c": 243.0},
        "rate_pid_state": {},
        "reset_temperature_pid": lambda reason="", target=None: calls[
            "temperature_resets"
        ].append((reason, target)),
        "set_active_feedback_controller": lambda label: calls["actions"].append(label),
        "_set_live_action_status": lambda message: calls["actions"].append(message),
        "log_control_decision": lambda **values: calls["logs"].append(values),
        "log_timestamp": lambda: (None, "", ""),
        "Fore": _Color(),
        "Style": _Color(),
    }
    exec(compile(isolated, str(path), "exec"), namespace)

    def label(mode=None):
        mode = mode or namespace["get_evaporation_control_mode"]()
        return {
            "temperature": "Temperature PID",
            "rate": "Rate PID",
            "compound": "Cascade rate → temperature PID",
        }[mode]

    namespace["evaporation_control_mode_label"] = label
    return namespace, calls


def test_live_mode_handover_preserves_current_and_discards_stale_actions() -> None:
    for path in PHASES:
        namespace, calls = _load_feedback_mode_functions(path)
        current_before = namespace["keysight_state"]["set_current_a"]

        changed = namespace["set_evaporation_control_mode"](
            "compound", "unit-test GUI selection"
        )

        assert changed is True
        assert namespace["get_evaporation_control_mode"]() == "compound"
        mode, generation = namespace["get_evaporation_control_mode_snapshot"]()
        assert mode == "compound"
        assert generation == 1
        assert namespace["keysight_state"]["set_current_a"] == current_before
        assert calls["rate_resets"]
        assert calls["temperature_resets"][-1][1] == 238.0
        assert calls["logs"][-1]["applied_delta"] == 0.0
        assert calls["logs"][-1]["current_before_a"] == current_before
        assert calls["logs"][-1]["current_after_a"] == current_before

        executed: list[str] = []
        stale = namespace["run_feedback_mode_action"](
            "temperature", 0, lambda: executed.append("stale")
        )
        current = namespace["run_feedback_mode_action"](
            "compound", generation, lambda: executed.append("current") or True
        )
        assert stale is None
        assert current is True
        assert executed == ["current"]


def test_phase01_robust_slope_window_constants_exist_before_runtime_use() -> None:
    phase_01 = PHASES[0].read_text(encoding="utf-8")
    assert "TEMP_SLOPE_WINDOW_S = 45.0" in phase_01
    assert "TEMP_SLOPE_MIN_SPAN_S = 20.0" in phase_01
    assert phase_01.index("TEMP_SLOPE_WINDOW_S = 45.0") < phase_01.index("def estimate_ck1_temp_slope_c_per_min")
    assert phase_01.index("TEMP_SLOPE_MIN_SPAN_S = 20.0") < phase_01.index("def estimate_ck1_temp_slope_c_per_min")


def test_qt_dashboard_button_helper_is_called_with_its_declared_signature() -> None:
    import ast

    tree = ast.parse(QT_DASHBOARD.read_text(encoding="utf-8"))
    bad_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "_button":
            if len(node.args) != 3:
                bad_calls.append((node.lineno, len(node.args)))
    assert bad_calls == []
