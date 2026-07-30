from __future__ import annotations

import pytest

from npg_chamber.common.evaporation_control import (
    RatePidConfig,
    RatePidController,
    robust_rate_average,
)


def controller() -> RatePidController:
    return RatePidController(
        RatePidConfig(
            kp_a_per_rate=0.020,
            ki_a_per_thickness=0.00020,
            kd_a_per_rate_slope=0.0,
            deadband_rate=0.02,
            max_up_step_a=0.002,
            max_down_step_a=0.005,
            integral_limit_thickness=25.0,
            min_current_a=0.0,
            max_current_a=0.670,
        )
    )


def test_robust_rate_average_rejects_one_isolated_spike() -> None:
    value = robust_rate_average([0.38, 0.40, 0.41, 1.20, 0.39, 0.40, 0.41], 7)
    assert value == pytest.approx(0.402, abs=0.005)


def test_sustained_high_rate_is_not_hidden_by_filter() -> None:
    value = robust_rate_average([1.1, 1.2, 1.15, 1.18, 1.21, 1.19, 1.17], 7)
    assert value == pytest.approx(1.178, abs=0.02)


def test_rate_pid_increases_slowly_below_target_and_reduces_faster_above_target() -> None:
    pid = controller()
    low = pid.update(
        target_rate=0.4,
        measured_rate=0.2,
        current_setpoint_a=0.60,
        now_s=0.0,
    )
    assert low.delta_a == pytest.approx(0.002)

    pid.reset()
    high = pid.update(
        target_rate=0.4,
        measured_rate=1.2,
        current_setpoint_a=0.60,
        now_s=0.0,
    )
    assert high.delta_a == pytest.approx(-0.005)


def test_rate_pid_holds_inside_deadband() -> None:
    decision = controller().update(
        target_rate=0.4,
        measured_rate=0.41,
        current_setpoint_a=0.60,
        now_s=0.0,
    )
    assert decision.inside_deadband is True
    assert decision.delta_a == 0.0


def test_compound_guard_tapers_positive_correction_near_temperature_ceiling() -> None:
    pid = controller()
    decision = pid.update(
        target_rate=0.4,
        measured_rate=0.2,
        current_setpoint_a=0.60,
        now_s=0.0,
        compound_temperature_guard=True,
        current_temperature_c=248.0,
        maximum_temperature_c=250.0,
        temperature_guard_band_c=5.0,
    )
    assert decision.temperature_limited is True
    assert decision.delta_a == pytest.approx(0.0008)


def test_compound_guard_never_increases_without_valid_temperature() -> None:
    decision = controller().update(
        target_rate=0.4,
        measured_rate=0.2,
        current_setpoint_a=0.60,
        now_s=0.0,
        compound_temperature_guard=True,
        current_temperature_c=None,
        maximum_temperature_c=250.0,
        temperature_guard_band_c=5.0,
    )
    assert decision.temperature_limited is True
    assert decision.delta_a == 0.0


def test_rate_pid_anti_windup_at_current_ceiling() -> None:
    pid = controller()
    first = pid.update(
        target_rate=0.4,
        measured_rate=0.1,
        current_setpoint_a=0.670,
        now_s=0.0,
    )
    second = pid.update(
        target_rate=0.4,
        measured_rate=0.1,
        current_setpoint_a=0.670,
        now_s=8.0,
    )
    assert first.delta_a == 0.0
    assert second.delta_a == 0.0
    assert second.integral_error_thickness == 0.0


def test_temperature_guard_does_not_wind_up_positive_integral() -> None:
    pid = controller()
    pid.update(
        target_rate=0.4,
        measured_rate=0.1,
        current_setpoint_a=0.60,
        now_s=0.0,
        compound_temperature_guard=True,
        current_temperature_c=250.0,
        maximum_temperature_c=250.0,
        temperature_guard_band_c=5.0,
    )
    blocked = pid.update(
        target_rate=0.4,
        measured_rate=0.1,
        current_setpoint_a=0.60,
        now_s=8.0,
        compound_temperature_guard=True,
        current_temperature_c=250.0,
        maximum_temperature_c=250.0,
        temperature_guard_band_c=5.0,
    )
    assert blocked.delta_a == 0.0
    assert blocked.integral_error_thickness == 0.0
