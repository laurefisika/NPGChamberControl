"""Reusable feedback-control helpers for CK-1 evaporation phases.

The module is intentionally hardware-agnostic.  It calculates conservative
current corrections from a filtered CK-1 QMB rate while the phase scripts keep
ownership of serial I/O, hard current/voltage stops, snapshots and shutdown.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import fmean
from typing import Iterable, Sequence


CONTROL_MODE_TEMPERATURE = "temperature"
CONTROL_MODE_RATE = "rate"
CONTROL_MODE_COMPOUND = "compound"
CONTROL_MODES = (
    CONTROL_MODE_TEMPERATURE,
    CONTROL_MODE_RATE,
    CONTROL_MODE_COMPOUND,
)


def robust_rate_average(values: Sequence[float] | Iterable[float], points: int) -> float | None:
    """Return a spike-resistant average of the newest finite non-negative rates.

    QMB rate streams can contain an isolated transient or negative display
    noise.  Negative values are treated as zero.  With five or more readings,
    one value from each extreme is discarded before averaging.  A sustained
    real increase therefore remains visible, while a single outlier does not
    command an unnecessary current change.
    """

    points = max(1, int(points))
    cleaned: list[float] = []
    for raw in list(values)[-points:]:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        cleaned.append(max(0.0, value))

    if len(cleaned) < points:
        return None

    if len(cleaned) >= 5:
        ordered = sorted(cleaned)
        cleaned = ordered[1:-1]
    return fmean(cleaned)


@dataclass(frozen=True)
class RatePidConfig:
    kp_a_per_rate: float
    ki_a_per_thickness: float
    kd_a_per_rate_slope: float
    deadband_rate: float
    max_up_step_a: float
    max_down_step_a: float
    integral_limit_thickness: float
    min_current_a: float = 0.0
    max_current_a: float = 0.670


@dataclass(frozen=True)
class RatePidDecision:
    delta_a: float
    raw_delta_a: float
    error_rate: float
    derivative_rate_per_s: float
    integral_error_thickness: float
    inside_deadband: bool
    temperature_limited: bool
    initialized: bool


class RatePidController:
    """Conservative asymmetric PID for CK-1 deposition-rate feedback.

    Positive and negative corrections have separate limits.  This allows a
    slow heat increase but a faster response to a real high-rate excursion.
    The derivative term is supported but can safely be left at zero for noisy
    QMB installations.
    """

    def __init__(self, config: RatePidConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.integral_error_thickness = 0.0
        self.last_error_rate: float | None = None
        self.last_time_s: float | None = None

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(value, high))

    def update(
        self,
        *,
        target_rate: float,
        measured_rate: float,
        current_setpoint_a: float,
        now_s: float,
        compound_temperature_guard: bool = False,
        current_temperature_c: float | None = None,
        maximum_temperature_c: float | None = None,
        temperature_guard_band_c: float = 0.0,
    ) -> RatePidDecision:
        target_rate = float(target_rate)
        measured_rate = float(measured_rate)
        current_setpoint_a = float(current_setpoint_a)
        now_s = float(now_s)
        error = target_rate - measured_rate

        previous_integral = self.integral_error_thickness
        initialized = self.last_time_s is not None and self.last_error_rate is not None
        if initialized:
            dt = max(0.1, now_s - float(self.last_time_s))
            derivative = (error - float(self.last_error_rate)) / dt
        else:
            # Apply proportional control immediately on handover so a genuine
            # high-rate condition is not held for one full control period.
            dt = 0.0
            derivative = 0.0

        inside_deadband = abs(error) <= max(0.0, self.config.deadband_rate)
        if inside_deadband:
            # Slowly discharge stored bias rather than chasing QMB noise.
            self.integral_error_thickness *= 0.90
            raw_delta = 0.0
            delta = 0.0
        else:
            integral_candidate = self.integral_error_thickness
            if initialized:
                integral_candidate += error * dt
            integral_candidate = self._clamp(
                integral_candidate,
                -abs(self.config.integral_limit_thickness),
                abs(self.config.integral_limit_thickness),
            )

            raw_delta = (
                self.config.kp_a_per_rate * error
                + self.config.ki_a_per_thickness * integral_candidate
                + self.config.kd_a_per_rate_slope * derivative
            )
            delta = self._clamp(
                raw_delta,
                -abs(self.config.max_down_step_a),
                abs(self.config.max_up_step_a),
            )

            # Current-bound anti-windup: do not accumulate error that can only
            # push farther into an already saturated limit.
            pushing_upper_limit = (
                current_setpoint_a >= self.config.max_current_a - 1e-12 and delta > 0
            )
            pushing_lower_limit = (
                current_setpoint_a <= self.config.min_current_a + 1e-12 and delta < 0
            )
            if not (pushing_upper_limit or pushing_lower_limit):
                self.integral_error_thickness = integral_candidate

        temperature_limited = False
        if compound_temperature_guard and delta > 0:
            if current_temperature_c is None or maximum_temperature_c is None:
                # Compound mode must never increase power without a valid
                # temperature limit signal.
                delta = 0.0
                temperature_limited = True
            else:
                guard_band = max(0.0, float(temperature_guard_band_c))
                remaining = float(maximum_temperature_c) - float(current_temperature_c)
                if remaining <= 0:
                    delta = 0.0
                    temperature_limited = True
                elif guard_band > 0 and remaining < guard_band:
                    delta *= self._clamp(remaining / guard_band, 0.0, 1.0)
                    temperature_limited = True

        if temperature_limited and error > 0:
            # Do not wind up the integral while the temperature supervisor is
            # deliberately refusing additional heating power.
            self.integral_error_thickness = previous_integral

        requested_current = self._clamp(
            current_setpoint_a + delta,
            self.config.min_current_a,
            self.config.max_current_a,
        )
        delta = requested_current - current_setpoint_a

        self.last_time_s = now_s
        self.last_error_rate = error
        return RatePidDecision(
            delta_a=delta,
            raw_delta_a=raw_delta,
            error_rate=error,
            derivative_rate_per_s=derivative,
            integral_error_thickness=self.integral_error_thickness,
            inside_deadband=inside_deadband,
            temperature_limited=temperature_limited,
            initialized=initialized,
        )
