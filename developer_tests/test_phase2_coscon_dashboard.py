from __future__ import annotations

import ast
import re
import threading
import sys
import types
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE2_PATH = (
    PROJECT_ROOT
    / "npg_chamber"
    / "legacy_scripts"
    / "02_sputtering_annealing_legacy.py"
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
    names = {"CosconStatus", "CosconMonitor", "CosconUDP"}
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
            "socket": fake_socket,
        }
    )
    sys.modules[module_name] = isolated_module
    try:
        exec(compile(module, str(PHASE2_PATH), "exec"), namespace)
    finally:
        sys.modules.pop(module_name, None)
    return namespace


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
    assert "COSCON energy target: {cfg.coscon_energy_v:.1f} V" in text
    assert "COSCON emission target: {cfg.coscon_emission_a * 1000:.3f} mA" in text

