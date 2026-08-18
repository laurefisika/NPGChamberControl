from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHASES = (
    ROOT / "npg_chamber/legacy_scripts/01_heat_up_calibration_legacy.py",
    ROOT / "npg_chamber/legacy_scripts/03_dp_dbba_evaporation_legacy.py",
)


def test_phase13_keysight_is_armed_at_zero_before_nonzero_start() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        configure_start = source.index("def configure_keysight_for_automation():")
        configure_end = source.index("\ndef stop_keysight_output", configure_start)
        configure = source[configure_start:configure_end]
        assert "keysight_set_current(KEYSIGHT_STARTUP_ZERO_CURRENT_A)" in configure
        assert configure.index("keysight_set_current(KEYSIGHT_STARTUP_ZERO_CURRENT_A)") < configure.index("keysight_write('OUTP ON')")
        assert configure.index("keysight_write('OUTP ON')") < configure.index("keysight_set_current(requested_start_current)")
        assert "KEYSIGHT_STARTUP_ENABLE_ATTEMPTS = 2" in source
        assert "KEYSIGHT_STARTUP_VERIFY_DELAY_S = 0.50" in source
        assert "post_start_status = keysight_protection_status()" not in configure


def test_phase13_keysight_configuration_precedes_thread_startup() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        main_start = source.index("def main():")
        main = source[main_start:]
        assert main.index("configure_keysight_for_automation()") < main.index("threads = [")
        automation_start = source.index("def automate_keysight_heating():")
        automation_end = source.index("\ndef process_controller", automation_start)
        automation = source[automation_start:automation_end]
        assert "configure_keysight_for_automation()" not in automation
        assert "started before zero-current output verification" in automation


def test_phase13_output_off_detection_is_startup_gated_and_confirmed() -> None:
    for path in PHASES:
        source = path.read_text(encoding="utf-8")
        monitor_start = source.index("def read_powersupply():")
        monitor_end = source.index("\ndef read_PID", monitor_start)
        monitor = source[monitor_start:monitor_end]
        assert "keysight_state.get('startup_verified', False)" in monitor
        assert "not keysight_state.get('startup_in_progress', False)" in monitor
        assert "KEYSIGHT_OUTPUT_OFF_CONFIRM_DELAY_S" in monitor
        assert "confirmation = keysight_protection_status()" in monitor

import ast
import types


def _load_configure_function(path: Path, statuses: list[dict[str, object]]):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "configure_keysight_for_automation"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)

    commands: list[str] = []
    currents: list[float] = []
    forced: list[str] = []
    banners: list[str] = []
    state = {
        "startup_in_progress": False,
        "startup_verified": False,
        "startup_attempts": 0,
        "startup_error": None,
        "automation_active": False,
        "set_current_a": None,
    }
    queue = list(statuses)

    def keysight_write(command: str) -> None:
        commands.append(command)

    def keysight_set_current(value: float) -> None:
        currents.append(float(value))
        state["set_current_a"] = float(value)

    def keysight_protection_status() -> dict[str, object]:
        if not queue:
            raise AssertionError("test status queue exhausted")
        return queue.pop(0)

    def force_keysight_zero_output(reason: str) -> None:
        forced.append(reason)
        state["set_current_a"] = 0.0
        state["automation_active"] = False
        state["startup_verified"] = False

    namespace = {
        "keysight_state": state,
        "keysight_write": keysight_write,
        "keysight_set_current": keysight_set_current,
        "keysight_set_voltage_limit": lambda value: commands.append(f"VOLT_LIMIT {value}"),
        "keysight_protection_status": keysight_protection_status,
        "force_keysight_zero_output": force_keysight_zero_output,
        "print_banner": banners.append,
        "clamp": lambda value, low, high: max(low, min(high, value)),
        "normal_current_cap_a": lambda: 0.660,
        "time": types.SimpleNamespace(sleep=lambda _: None, time=lambda: 1234.5),
        "KEYSIGHT_RANGE": "LOW",
        "KEYSIGHT_INSTRUMENT_OVP_V": 2.50,
        "KEYSIGHT_INSTRUMENT_OCP_A": 0.685,
        "KEYSIGHT_VOLTAGE_LIMIT_V": 2.30,
        "KEYSIGHT_STARTUP_ZERO_CURRENT_A": 0.0,
        "KEYSIGHT_STARTUP_ENABLE_ATTEMPTS": 2,
        "KEYSIGHT_STARTUP_VERIFY_DELAY_S": 0.50,
        "KEYSIGHT_START_CURRENT_A": 0.005,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["configure_keysight_for_automation"], commands, currents, forced, banners, state


def _ok_status() -> dict[str, object]:
    return {"output_on": True, "ocp_tripped": False, "ovp_tripped": False}


def test_zero_current_startup_sequence_executes_before_first_ramp_setpoint() -> None:
    for path in PHASES:
        configure, commands, currents, forced, banners, state = _load_configure_function(
            path, [_ok_status()]
        )
        configure()
        assert currents == [0.0, 0.005]
        assert commands.index("OUTP ON") > commands.index("OUTP OFF")
        assert not forced
        assert state["startup_verified"] is True
        assert state["automation_active"] is True
        assert state["startup_attempts"] == 1
        assert any("confirmed at 0.000 A" in banner for banner in banners)


def test_zero_current_startup_retries_output_enable_at_zero() -> None:
    off = {"output_on": False, "ocp_tripped": False, "ovp_tripped": False}
    for path in PHASES:
        configure, commands, currents, forced, _, state = _load_configure_function(
            path, [off, _ok_status()]
        )
        configure()
        assert commands.count("OUTP ON") == 2
        assert currents[:2] == [0.0, 0.0]
        assert currents[-1] == 0.005
        assert not forced
        assert state["startup_attempts"] == 2


def test_zero_current_startup_fails_safe_after_one_retry() -> None:
    off = {"output_on": False, "ocp_tripped": False, "ovp_tripped": False}
    for path in PHASES:
        configure, commands, currents, forced, _, state = _load_configure_function(
            path, [off, off]
        )
        try:
            configure()
        except RuntimeError as exc:
            assert "failed after 2 attempts (one retry)" in str(exc)
        else:
            raise AssertionError("startup should have failed")
        assert commands.count("OUTP ON") == 2
        assert currents[0] == 0.0
        assert forced
        assert state["startup_verified"] is False
        assert state["automation_active"] is False
