from __future__ import annotations

import ast
import copy
import math
import os
import queue
import re
import threading
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE2_PATH = (
    PROJECT_ROOT
    / "npg_chamber"
    / "phase_scripts"
    / "02_sputtering_annealing.py"
)


class _FakeSocket:
    def __init__(self, owner: "_FakeSocketModule") -> None:
        self.owner = owner

    def __enter__(self) -> "_FakeSocket":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def settimeout(self, timeout_s: float) -> None:
        self.owner.timeout_s = timeout_s

    def sendto(self, payload: bytes, target: tuple[str, int]) -> None:
        self.owner.sent.append((payload, target))

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        return self.owner.replies.pop(0), (self.owner.ip, self.owner.port)


class _FakeSocketModule:
    AF_INET = object()
    SOCK_DGRAM = object()

    def __init__(self, replies: list[bytes], ip: str, port: int) -> None:
        self.replies = replies
        self.ip = ip
        self.port = port
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.timeout_s: float | None = None

    def socket(self, *_args, **_kwargs) -> _FakeSocket:
        return _FakeSocket(self)


def _load_isolated_coscon_classes(fake_socket: _FakeSocketModule) -> dict:
    tree = ast.parse(PHASE2_PATH.read_text(encoding="utf-8"))
    selected = []
    names = {"CosconStatus", "CosconMonitor", "CosconDiagnostics", "CosconUDP"}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in names:
            selected.append(node)

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    module_name = "phase2_coscon_test"
    isolated_module = types.ModuleType(module_name)
    namespace = isolated_module.__dict__
    namespace.update(
        {
            "__name__": module_name,
            "dataclass": dataclass,
            "re": re,
            "threading": threading,
            "time": __import__("time"),
            "socket": fake_socket,
        }
    )
    sys.modules[module_name] = isolated_module
    try:
        exec(compile(module, str(PHASE2_PATH), "exec"), namespace)
    finally:
        sys.modules.pop(module_name, None)
    return namespace


def _load_isolated_activation_controller() -> type:
    tree = ast.parse(PHASE2_PATH.read_text(encoding="utf-8"))
    controller = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SputterAnnealController"
    )
    selected = copy.deepcopy(controller)
    wanted = {"_is_recoverable_activation_overload", "coscon_start_sputter"}
    selected.body = [
        node
        for node in selected.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            selected,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "__name__": "phase2_activation_test",
        "math": math,
        "re": re,
        "threading": threading,
        "time": __import__("time"),
        "banner": lambda *_args, **_kwargs: None,
        "info": lambda *_args, **_kwargs: None,
        "COSCON_ACTIVATION_QUIET_S": 1.5,
    }
    exec(compile(module, str(PHASE2_PATH), "exec"), namespace)
    return namespace["SputterAnnealController"]


def _load_isolated_ui_client() -> type:
    tree = ast.parse(PHASE2_PATH.read_text(encoding="utf-8"))
    ui_client = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UnifiedUIClient"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            ui_client,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "__name__": "phase2_ui_test",
        "Callable": object,
        "Optional": object,
        "os": os,
        "queue": queue,
        "sys": sys,
        "threading": threading,
        "time": __import__("time"),
    }
    exec(compile(module, str(PHASE2_PATH), "exec"), namespace)
    return namespace["UnifiedUIClient"]


def test_integrated_coscon_uses_real_carriage_return_and_parses_status() -> None:
    fake_socket = _FakeSocketModule(
        [
            b'GetStatus OK: Mode=Standby Interlock=Ok Details="Standby"\r',
            b"GetMonitorValues OK: VEnergy=2.250000e+03 "
            b"IFilament=4.530000e+00 IEmission=1.000000e-02\r",
        ],
        ip="192.168.236.186",
        port=2005,
    )
    namespace = _load_isolated_coscon_classes(fake_socket)
    client = namespace["CosconUDP"]("192.168.236.186", 2005, 2.0)

    status = client.status()
    monitor = client.monitor()

    assert fake_socket.sent[0][0] == b"GetStatus\r"
    assert fake_socket.sent[1][0] == b"GetMonitorValues\r"
    assert status.mode == "Standby"
    assert status.interlock == "Ok"
    assert monitor.energy_v == 2250.0
    assert monitor.emission_a == 0.010


def test_coscon_reset_and_uptime_use_documented_udp_commands() -> None:
    fake_socket = _FakeSocketModule(
        [
            b"Uptime OK: 135\r",
            b"Reset OK\r",
        ],
        ip="192.168.236.186",
        port=2005,
    )
    namespace = _load_isolated_coscon_classes(fake_socket)
    client = namespace["CosconUDP"]("192.168.236.186", 2005, 2.0)

    assert client.uptime_s() == 135
    assert client.send("Reset") == "Reset OK"
    assert fake_socket.sent[0][0] == b"Uptime\r"
    assert fake_socket.sent[1][0] == b"Reset\r"


def test_coscon_activation_keeps_validation_quiet_interval_and_operate_atomic() -> None:
    fake_socket = _FakeSocketModule(
        [
            b"ValidateOperateTarget OK\r",
            b"SwitchToOperate OK\r",
        ],
        ip="192.168.236.186",
        port=2005,
    )
    namespace = _load_isolated_coscon_classes(fake_socket)
    client = namespace["CosconUDP"]("192.168.236.186", 2005, 2.0)

    client.activate(0.010, 2250.0, quiet_s=0.0)

    assert [payload for payload, _target in fake_socket.sent] == [
        b"ValidateOperateTarget Emission=1.000000e-02 Energy=2250\r",
        b"SwitchToOperate Emission=1.000000e-02 Energy=2250\r",
    ]


def test_mode_error_is_a_valid_status_reply_not_a_rejected_command() -> None:
    fake_socket = _FakeSocketModule(
        [
            b'GetStatus OK: Mode=Error Interlock=Ok '
            b'Details="Error: HV-Module Energy Overload"\r'
        ],
        ip="192.168.236.186",
        port=2005,
    )
    namespace = _load_isolated_coscon_classes(fake_socket)
    client = namespace["CosconUDP"]("192.168.236.186", 2005, 2.0)

    status = client.status()

    assert status.mode == "Error"
    assert status.interlock == "Ok"
    assert "HV-Module Energy Overload" in status.details


def test_phase2_dashboard_has_no_embedded_coscon_page_and_dynamic_actions() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")

    assert "<iframe" not in text
    assert "__COSCON_URL__" not in text
    assert 'id="actionButtons"' in text
    assert "What to do now" in text
    assert "estimated_timed_remaining_s" in text
    assert "COSCON_STANDBY" in text
    assert "import math" in text
    assert '"coscon_energy_v"' in text
    assert '"phase_remaining_s"' in text
    assert 'id="pidControlCard"' in text
    assert "PID SV changes are available only during the oven ramp or anneal hold." in text

    # Regression checks for the over-escaping that caused v0.9.13 to send
    # literal backslash-r characters and fail every GetStatus query.
    assert 'payload = (command + "\\\\r")' not in text
    assert 'MODE_RE = re.compile(r"\\\\b' not in text


def test_phase2_dashboard_v0916_is_task_focused_and_degas_timing_is_unambiguous() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")

    assert "grid-template-columns:minmax(570px,1.28fr)" in text
    assert "What to do now" in text
    assert "Critical process values" in text
    assert "Task-focused operator view" in text
    assert '<summary>Current-cycle workflow</summary>' in text
    assert '<summary>Auxiliary diagnostics</summary>' in text
    assert '<summary>Safety reminders</summary>' in text
    assert 'id="interlockVal"' in text
    assert "Known timed work left" in text
    assert "Degas safety timeout left" in text
    assert "expected 20:00" not in text
    assert "degas_expected_s" not in text
    assert "expected_degassing_minutes" not in text

def test_phase2_uses_run_only_coscon_target_fields_everywhere() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")
    assert "self.cfg.coscon_energy_v" in text
    assert "self.cfg.coscon_emission_a" in text
    assert "ValidateOperateTarget Emission={emission_a:.6e}" in text
    assert "SwitchToOperate Emission={emission_a:.6e}" in text
    assert 'print("\nCurrent configuration:")' not in text
    assert "format_override_summary" not in text
    assert "def activate(" in text
    assert "COSCON_ACTIVATION_QUIET_S = 1.5" in text



def test_phase2_parses_optional_coscon_diagnostic_values() -> None:
    fake_socket = _FakeSocketModule(
        [
            b"GetDiagnosticValues OK: IEnergy=1.230000e-02 "
            b"VAnode=1.840000e+02 VRepeller=-2.550000e+01\r",
        ],
        ip="192.168.236.186",
        port=2005,
    )
    namespace = _load_isolated_coscon_classes(fake_socket)
    client = namespace["CosconUDP"]("192.168.236.186", 2005, 2.0)

    diagnostic = client.diagnostics()

    assert fake_socket.sent[0][0] == b"GetDiagnosticValues\r"
    assert diagnostic.energy_current_a == 0.0123
    assert diagnostic.anode_voltage_v == 184.0
    assert diagnostic.repeller_voltage_v == -25.5


def test_phase2_rechecks_emission_before_abort_and_displays_new_values() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")

    assert "coscon_emission_fault_samples: int = 3" in text
    assert "coscon_emission_recheck_s: float = 0.5" in text
    assert "Consecutive bad reading" in text
    assert "COSCON emission returned inside tolerance" in text
    assert "emission_bad_samples >= self.cfg.coscon_emission_fault_samples" in text
    assert 'id="energyCurrentVal"' in text
    assert 'id="anodeVoltageVal"' in text
    assert 'id="repellerVoltageVal"' in text
    assert 'self.coscon.diagnostics()' in text
    assert 'self.logger.log_snapshot(self.state.snapshot(), note=warning_note)' in text


def test_phase2_supports_explicit_continuation_without_initial_degas() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")

    assert "start_without_degassing: bool = False" in text
    assert "cycle == 1 and not self.cfg.start_without_degassing" in text
    assert "INITIAL DEGAS SKIPPED" in text
    assert "skipping Degas was not explicitly confirmed" in text
    assert '"start_without_degassing": self.cfg.start_without_degassing' in text
    assert "Skipped by launcher setting for this continuation run" in text


def test_phase2_skip_degas_remains_fully_automatic_from_off() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")
    assert 'return {"off", "standby"}' in text
    assert 'self._safe_coscon_modes_before_operate(cycle)' in text
    assert 'SwitchToOperate Emission=' in text
    assert 'Standby selected' not in text
    assert 'local COSCON' not in text
    assert 'COSCON must remain in Standby during pressure conditioning' not in text


def test_phase2_repeated_activation_overload_has_guarded_reset_recovery() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")

    assert "coscon_activation_overload_retries: int = 1" in text
    assert "coscon_activation_recovery_wait_s: float = 8.0" in text
    assert "coscon_activation_reset_retries: int = 1" in text
    assert "coscon_reset_reconnect_timeout_s: float = 60.0" in text
    assert "coscon_reset_safe_samples: int = 3" in text
    assert "coscon_post_reset_conditioning_s: float = 60.0" in text
    assert "def _recover_activation_overload_with_reset" in text
    assert 'self._wait_for_token(\n            "c"' in text
    assert 'self.coscon.send("Reset")' in text
    assert "safe_samples >= required_safe_samples" in text
    assert "post_reset_uptime < pre_reset_uptime" in text
    assert 'self._wait_for_token(\n            "o"' in text
    assert 'check.mode.lower() != "off"' in text
    assert "self._coscon_polling_paused.set()" in text
    assert "self._coscon_polling_paused.clear()" in text


def test_activation_sequence_keeps_short_retry_then_reset_then_final_attempt() -> None:
    controller_type = _load_isolated_activation_controller()
    controller = object.__new__(controller_type)
    events: list[str] = []

    standby = SimpleNamespace(
        mode="Standby", interlock="Ok", details="Standby", raw="standby"
    )
    overload = SimpleNamespace(
        mode="Error",
        interlock="Ok",
        details="Error: HV-Module Energy Overload",
        raw="overload",
    )
    operating = SimpleNamespace(
        mode="Operating", interlock="Ok", details="Operating", raw="operating"
    )
    statuses = iter([standby, overload, overload, operating, operating])

    class FakeCoscon:
        def __init__(self):
            self.lock = threading.RLock()

        def status(self):
            return next(statuses)

        def monitor(self):
            return SimpleNamespace(energy_v=2250.0, emission_a=0.010)

        def activate(self, _emission, _energy, *, quiet_s):
            events.extend(["validate", "operate"])

    controller.cfg = SimpleNamespace(
        coscon_activation_overload_retries=1,
        coscon_activation_reset_retries=1,
        coscon_emission_a=0.010,
        coscon_energy_v=2250.0,
        operate_transition_timeout_s=2,
        coscon_energy_tolerance_v=50.0,
        coscon_emission_tolerance_a=0.001,
        coscon_stable_samples=1,
    )
    controller.coscon = FakeCoscon()
    controller.state = SimpleNamespace(snapshot=lambda: {})
    controller.logger = SimpleNamespace(log_snapshot=lambda *_args, **_kwargs: None)
    controller._coscon_polling_paused = threading.Event()
    controller.coscon_activation_requested = False
    controller._set_stage = lambda *_args, **_kwargs: None
    controller._safe_coscon_modes_before_operate = lambda _cycle: {"standby"}
    controller._require_pressure = lambda **_kwargs: 2.0e-5
    controller._set_phase_timer = lambda *_args, **_kwargs: None
    controller._clear_phase_timer = lambda *_args, **_kwargs: None
    controller._poll_ui_background = lambda: None
    controller._recover_activation_overload = (
        lambda *_args, **_kwargs: events.append("8-second retry") or True
    )
    controller._recover_activation_overload_with_reset = (
        lambda *_args, **_kwargs: events.append("Reset recovery")
    )

    controller.coscon_start_sputter(1)

    assert events == [
        "validate",
        "operate",
        "8-second retry",
        "validate",
        "operate",
        "Reset recovery",
        "validate",
        "operate",
    ]


def test_activation_does_not_reset_for_unrelated_coscon_error() -> None:
    controller_type = _load_isolated_activation_controller()
    controller = object.__new__(controller_type)
    statuses = iter(
        [
            SimpleNamespace(mode="Standby", interlock="Ok", details="Standby", raw="standby"),
            SimpleNamespace(
                mode="Error",
                interlock="Ok",
                details="Error: Filament failure",
                raw="filament failure",
            ),
        ]
    )

    controller.cfg = SimpleNamespace(
        coscon_activation_overload_retries=1,
        coscon_activation_reset_retries=1,
        coscon_emission_a=0.010,
        coscon_energy_v=2250.0,
        operate_transition_timeout_s=2,
    )
    controller.coscon = SimpleNamespace(
        lock=threading.RLock(),
        status=lambda: next(statuses),
        monitor=lambda: SimpleNamespace(energy_v=0.0, emission_a=0.0),
        activate=lambda *_args, **_kwargs: None,
    )
    controller.state = SimpleNamespace(snapshot=lambda: {})
    controller.logger = SimpleNamespace(log_snapshot=lambda *_args, **_kwargs: None)
    controller._coscon_polling_paused = threading.Event()
    controller.coscon_activation_requested = False
    controller._set_stage = lambda *_args, **_kwargs: None
    controller._safe_coscon_modes_before_operate = lambda _cycle: {"standby"}
    controller._require_pressure = lambda **_kwargs: 2.0e-5
    controller._set_phase_timer = lambda *_args, **_kwargs: None
    controller._poll_ui_background = lambda: None
    controller._recover_activation_overload = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("short recovery must not run")
    )
    controller._recover_activation_overload_with_reset = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(AssertionError("Reset recovery must not run"))

    import pytest

    with pytest.raises(RuntimeError, match="COSCON device error: Error: Filament failure"):
        controller.coscon_start_sputter(1)


def test_latched_short_recovery_allows_only_one_post_reset_operate_attempt() -> None:
    import pytest

    controller_type = _load_isolated_activation_controller()
    controller = object.__new__(controller_type)
    events: list[str] = []
    standby = SimpleNamespace(mode="Standby", interlock="Ok", details="Standby", raw="standby")
    overload = SimpleNamespace(
        mode="Error",
        interlock="Ok",
        details="Error: HV-Module Energy Overload",
        raw="overload",
    )
    # Initial state, first failed Operate, refresh before Reset, failed final Operate.
    statuses = iter([standby, overload, overload, overload])

    controller.cfg = SimpleNamespace(
        coscon_activation_overload_retries=1,
        coscon_activation_reset_retries=1,
        coscon_emission_a=0.010,
        coscon_energy_v=2250.0,
        operate_transition_timeout_s=2,
    )
    controller.coscon = SimpleNamespace(
        lock=threading.RLock(),
        status=lambda: next(statuses),
        monitor=lambda: SimpleNamespace(energy_v=0.0, emission_a=0.0),
        activate=lambda *_args, **_kwargs: events.extend(["validate", "operate"]),
    )
    controller.state = SimpleNamespace(snapshot=lambda: {})
    controller.logger = SimpleNamespace(log_snapshot=lambda *_args, **_kwargs: None)
    controller._coscon_polling_paused = threading.Event()
    controller.coscon_activation_requested = False
    controller._set_stage = lambda *_args, **_kwargs: None
    controller._safe_coscon_modes_before_operate = lambda _cycle: {"standby"}
    controller._require_pressure = lambda **_kwargs: 2.0e-5
    controller._set_phase_timer = lambda *_args, **_kwargs: None
    controller._poll_ui_background = lambda: None
    controller._recover_activation_overload = (
        lambda *_args, **_kwargs: events.append("latched after 8 seconds") or False
    )
    controller._recover_activation_overload_with_reset = (
        lambda *_args, **_kwargs: events.append("Reset recovery")
    )

    with pytest.raises(RuntimeError, match="no approved recovery remains"):
        controller.coscon_start_sputter(1)

    assert events == [
        "validate",
        "operate",
        "latched after 8 seconds",
        "Reset recovery",
        "validate",
        "operate",
    ]


def test_phase2_waits_for_verified_pywebview_backend() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")
    assert 'ready_event.wait(timeout=0.10)' in text
    assert 'webview.start(mark_ready, debug=False)' in text
    assert 'Windows backend was verified' in text


def test_phase2_manual_tokens_cannot_be_stolen_by_background_polling() -> None:
    text = PHASE2_PATH.read_text(encoding="utf-8")
    assert "self._pending_tokens" in text
    assert "self._pending_tokens_lock" in text
    assert "self.event_q.get(timeout=UI_TOKEN_POLL_S)" in text
    assert "OPERATOR_PROMPT_TIMEOUT_S = 15 * 60" in text

    ui_client_type = _load_isolated_ui_client()
    client = ui_client_type(True, "test", 100, 100)

    class FakeProcess:
        def is_alive(self) -> bool:
            return True

    client.process = FakeProcess()
    client.command_q = queue.Queue()
    client.event_q = queue.Queue()
    client.event_q.put(("token", "c"))

    # This represents the telemetry thread polling just before the controller
    # reaches the manual confirmation wait.
    client.poll_background_tokens()
    assert client.wait_for_token(["c"], "close valve") == "c"


def test_phase2_abort_stolen_by_background_polling_is_propagated() -> None:
    ui_client_type = _load_isolated_ui_client()
    client = ui_client_type(True, "test", 100, 100)

    class FakeProcess:
        def is_alive(self) -> bool:
            return True

    client.process = FakeProcess()
    client.command_q = queue.Queue()
    client.event_q = queue.Queue()
    client.event_q.put(("token", "abort"))
    client.poll_background_tokens()

    import pytest

    with pytest.raises(KeyboardInterrupt, match="Abort requested"):
        client.wait_for_token(["c"], "close valve")


def test_phase2_dashboard_failure_has_console_fallback() -> None:
    ui_client_type = _load_isolated_ui_client()
    client = ui_client_type(True, "test", 100, 100)

    class DeadProcess:
        def is_alive(self) -> bool:
            return False

    client.process = DeadProcess()
    client.command_q = queue.Queue()
    client.event_q = queue.Queue()
    client._read_console_token = lambda: "c"

    assert client.wait_for_token(["c"], "close valve") == "c"
