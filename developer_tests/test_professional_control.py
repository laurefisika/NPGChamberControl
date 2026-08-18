from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from npg_chamber.common.evaporation_control import RatePidConfig, RatePidController
from npg_chamber.common.professional_control import (
    CascadeConfig,
    CascadeRateController,
    StableBandTracker,
    StableConditionTracker,
    TemperaturePidConfig,
    TemperaturePidController,
    exact_calibration_ratio,
    robust_rate_from_thickness,
)


def test_robust_thickness_slope_rejects_isolated_outlier() -> None:
    t0 = datetime(2026, 8, 4, 12, 0, 0)
    times = [t0 + timedelta(seconds=i) for i in range(61)]
    thickness = [0.4 * i for i in range(61)]
    thickness[30] += 20.0
    estimate = robust_rate_from_thickness(
        times,
        thickness,
        window_s=60,
        min_points=30,
        min_span_s=30,
    )
    assert estimate.valid
    assert estimate.value_per_s == pytest.approx(0.4, abs=0.01)
    assert estimate.rejected_points >= 1


def temperature_pid() -> TemperaturePidController:
    return TemperaturePidController(
        TemperaturePidConfig(
            kp=0.002,
            ki=0.00003,
            kd=0.003,
            deadband_c=1.0,
            integral_limit_c_s=250,
            integral_active_error_c=5.0,
            derivative_tau_s=20.0,
            max_up_slew_a_per_min=0.015,
            max_down_slew_a_per_min=0.015,
            max_current_a=0.660,
        )
    )


def test_temperature_pid_derivative_is_on_measurement_not_setpoint() -> None:
    pid = temperature_pid()
    pid.reset(now_s=0.0, measurement_c=240.0, target_c=240.0, bumpless=True)
    decision = pid.update(target_c=245.0, measurement_c=240.0, current_a=0.60, now_s=8.0)
    assert decision.derivative_c_per_s == pytest.approx(0.0)
    assert decision.d_a == pytest.approx(0.0)


def test_temperature_pid_freezes_integral_while_slew_limited() -> None:
    pid = temperature_pid()
    pid.reset(now_s=0.0, measurement_c=220.0, target_c=240.0, bumpless=False)
    decision = pid.update(target_c=240.0, measurement_c=220.0, current_a=0.50, now_s=8.0)
    assert decision.slew_limited
    assert decision.integral_frozen
    assert decision.integral_c_s == pytest.approx(0.0)


def test_cascade_outer_loop_changes_temperature_target_not_current() -> None:
    cascade = CascadeRateController(
        CascadeConfig(
            kp_c_per_rate=2.0,
            ki_c_per_thickness=0.005,
            deadband_rate=0.02,
            integral_limit_thickness=20.0,
            max_up_c_per_min=0.5,
            max_down_c_per_min=0.75,
            trim_limit_c=8.0,
            settling_s=30.0,
            trend_hold_threshold_per_s=0.0015,
        )
    )
    cascade.reset(base_target_c=242.0, now_s=0.0)
    first = cascade.update(
        target_rate=0.4,
        measured_rate=0.2,
        rate_trend_per_s=0.0,
        base_target_c=242.0,
        max_temp_c=250.0,
        now_s=40.0,
    )
    assert 242.0 < first.target_c <= 242.34
    second = cascade.update(
        target_rate=0.4,
        measured_rate=0.2,
        rate_trend_per_s=0.0,
        base_target_c=242.0,
        max_temp_c=250.0,
        now_s=50.0,
    )
    assert second.settling
    assert second.delta_c == pytest.approx(0.0)


def test_stable_band_tracker_requires_continuous_time_and_resets() -> None:
    tracker = StableBandTracker(tolerance=2.0, duration_s=30.0)
    assert not tracker.update(200.5, 200.0, 0.0)
    assert not tracker.update(201.0, 200.0, 20.0)
    assert not tracker.update(203.0, 200.0, 25.0)
    assert not tracker.update(200.0, 200.0, 40.0)
    assert tracker.update(201.0, 200.0, 70.0)


def test_stable_condition_tracker_requires_continuous_true_condition() -> None:
    tracker = StableConditionTracker(duration_s=60.0)
    assert not tracker.update(True, 0.0)
    assert not tracker.update(True, 30.0)
    assert not tracker.update(False, 40.0)
    assert not tracker.update(True, 100.0)
    assert tracker.update(True, 160.0)
    assert tracker.elapsed(170.0) == pytest.approx(70.0)


def test_direct_rate_pid_does_not_wind_up_when_step_limited() -> None:
    pid = RatePidController(
        RatePidConfig(
            kp_a_per_rate=0.02,
            ki_a_per_thickness=0.0002,
            kd_a_per_rate_slope=0.0,
            deadband_rate=0.02,
            max_up_step_a=0.001,
            max_down_step_a=0.0015,
            integral_limit_thickness=25.0,
            max_current_a=0.660,
        )
    )
    pid.update(target_rate=0.4, measured_rate=0.0, current_setpoint_a=0.50, now_s=0.0)
    decision = pid.update(target_rate=0.4, measured_rate=0.0, current_setpoint_a=0.50, now_s=20.0)
    assert decision.delta_a == pytest.approx(0.001)
    assert decision.integral_error_thickness == pytest.approx(0.0)


def test_cascade_handover_below_trim_range_moves_only_by_slew() -> None:
    cascade = CascadeRateController(
        CascadeConfig(
            kp_c_per_rate=1.5,
            ki_c_per_thickness=0.0,
            deadband_rate=0.04,
            integral_limit_thickness=20.0,
            max_up_c_per_min=0.2,
            max_down_c_per_min=0.2,
            trim_limit_c=5.0,
            settling_s=0.0,
            trend_hold_threshold_per_s=0.0005,
        )
    )
    cascade.reset(base_target_c=242.0, current_target_c=230.0, now_s=0.0)
    decision = cascade.update(
        target_rate=0.4,
        measured_rate=0.1,
        rate_trend_per_s=0.0,
        base_target_c=242.0,
        max_temp_c=250.0,
        now_s=60.0,
    )
    # The lower trim bound is 237 C, but handover must not jump directly there.
    assert decision.target_c == pytest.approx(230.2)


def test_cascade_live_guide_edit_is_bumpless() -> None:
    cascade = CascadeRateController(
        CascadeConfig(
            kp_c_per_rate=1.5,
            ki_c_per_thickness=0.0,
            deadband_rate=0.03,
            integral_limit_thickness=20.0,
            max_up_c_per_min=0.4,
            max_down_c_per_min=0.4,
            trim_limit_c=8.0,
            settling_s=0.0,
            trend_hold_threshold_per_s=0.0005,
        )
    )
    cascade.reset(base_target_c=242.0, current_target_c=240.0, now_s=0.0)
    decision = cascade.update(
        target_rate=0.4,
        measured_rate=0.4,
        rate_trend_per_s=0.0,
        base_target_c=245.0,
        max_temp_c=250.0,
        now_s=60.0,
    )
    assert decision.target_c == pytest.approx(240.0)


def test_qmb_guard_rejects_impossible_rate_and_reset() -> None:
    from npg_chamber.common.professional_control import QmbGuardConfig, QmbSignalGuard

    guard = QmbSignalGuard(
        QmbGuardConfig(
            max_abs_rate_a_per_s=10.0,
            max_thickness_rate_a_per_s=10.0,
            min_allowed_thickness_jump_a=5.0,
        )
    )
    assert guard.check_thickness(569.0, 0.0).accepted
    rate_decision = guard.check_rate(-21296.0)
    assert not rate_decision.accepted
    reset_decision = guard.check_thickness(-1560.0, 1.0)
    assert not reset_decision.accepted
    assert reset_decision.previous_value == pytest.approx(569.0)
    # A later valid point is still compared with the last trusted sample.
    assert guard.check_thickness(569.5, 2.0).accepted


def test_cascade_slew_budget_accumulates_during_settling() -> None:
    cascade = CascadeRateController(
        CascadeConfig(
            kp_c_per_rate=5.0,
            ki_c_per_thickness=0.0,
            deadband_rate=0.01,
            integral_limit_thickness=20.0,
            max_up_c_per_min=0.2,
            max_down_c_per_min=0.2,
            trim_limit_c=8.0,
            settling_s=240.0,
            trend_hold_threshold_per_s=0.0005,
            max_step_c=0.75,
        )
    )
    cascade.reset(base_target_c=242.0, current_target_c=238.0, now_s=0.0)
    # The first post-handover decision is permitted once the 240 s settling
    # interval has elapsed. Its 0.8 C slew allowance is capped to 0.75 C/action.
    first = cascade.update(
        target_rate=0.8, measured_rate=0.0, rate_trend_per_s=0.0,
        base_target_c=242.0, max_temp_c=250.0, now_s=240.0,
    )
    assert first.delta_c == pytest.approx(0.75)
    for now_s in (300.0, 360.0, 420.0):
        held = cascade.update(
            target_rate=0.8, measured_rate=0.0, rate_trend_per_s=0.0,
            base_target_c=242.0, max_temp_c=250.0, now_s=now_s,
        )
        assert held.delta_c == pytest.approx(0.0)
        assert held.settling
    # At 480 s, the new slew allowance is based on 240 s since the last
    # applied target action, not on only 60 s since the last held evaluation.
    after_settling = cascade.update(
        target_rate=0.8, measured_rate=0.0, rate_trend_per_s=0.0,
        base_target_c=242.0, max_temp_c=250.0, now_s=480.0,
    )
    assert after_settling.delta_c == pytest.approx(0.75)


def test_cascade_per_action_target_step_cap_applies_both_directions() -> None:
    cascade = CascadeRateController(
        CascadeConfig(
            kp_c_per_rate=10.0,
            ki_c_per_thickness=0.0,
            deadband_rate=0.01,
            integral_limit_thickness=20.0,
            max_up_c_per_min=5.0,
            max_down_c_per_min=5.0,
            trim_limit_c=8.0,
            settling_s=0.0,
            trend_hold_threshold_per_s=0.0,
            max_step_c=0.75,
        )
    )
    cascade.reset(base_target_c=242.0, current_target_c=242.0, now_s=0.0)
    up = cascade.update(
        target_rate=1.0, measured_rate=0.0, rate_trend_per_s=0.0,
        base_target_c=242.0, max_temp_c=250.0, now_s=60.0,
    )
    assert up.delta_c == pytest.approx(0.75)
    down = cascade.update(
        target_rate=0.0, measured_rate=1.0, rate_trend_per_s=0.0,
        base_target_c=242.0, max_temp_c=250.0, now_s=120.0,
    )
    assert down.delta_c == pytest.approx(-0.75)


def test_exact_calibration_ratio_interpolates_crossing_and_checks_linearity() -> None:
    t0 = datetime(2026, 8, 5, 10, 0, 0)
    times = [t0 + timedelta(seconds=i) for i in range(12)]
    sample = [3.0 + 0.11 * i for i in range(12)]
    ck1 = [100.0 + 11.77 * i for i in range(12)]
    result = exact_calibration_ratio(
        sample_times=times, sample_thickness=sample,
        ck1_times=times, ck1_thickness=ck1,
        sample_baseline_a=3.0, ck1_baseline_a=100.0,
        sample_start_time=t0, ck1_start_time=t0,
        target_sample_a=1.0, minimum_linearity_r2=0.985,
    )
    assert result.ratio == pytest.approx(107.0, abs=1e-5)
    assert result.ck1_relative_at_crossing_a == pytest.approx(107.0, abs=1e-5)
    assert result.linearity_r2 == pytest.approx(1.0)
    assert result.quality_pass

    nonlinear = exact_calibration_ratio(
        sample_times=times, sample_thickness=sample,
        ck1_times=times, ck1_thickness=[100.0 + 9.0 * i + 0.55 * i * i for i in range(12)],
        sample_baseline_a=3.0, ck1_baseline_a=100.0,
        sample_start_time=t0, ck1_start_time=t0,
        target_sample_a=1.0, minimum_linearity_r2=0.9999,
    )
    assert not nonlinear.quality_pass
    assert "REVIEW / REPEAT RECOMMENDED" in nonlinear.quality_message
