"""Professional signal estimation and cascade feedback helpers.

These helpers are hardware-agnostic and deterministic.  Phase scripts retain
ownership of serial I/O, hard limits, watchdogs and operator workflow.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import csv
import math
import os
import threading
from statistics import fmean, median
from typing import Iterable, Sequence

MOLECULE_PROFILE_NORMAL = "normal"
MOLECULE_PROFILE_FRESH = "fresh_post_refill"


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _seconds(value):
    if isinstance(value, datetime):
        return value.timestamp()
    fn = getattr(value, "timestamp", None)
    if callable(fn):
        try:
            return float(fn())
        except Exception:
            return None
    return _finite(value)


def robust_median(values: Sequence[float] | Iterable[float], points: int) -> float | None:
    points = max(1, int(points))
    clean = [v for v in (_finite(x) for x in list(values)[-points:]) if v is not None]
    return float(median(clean)) if len(clean) >= points else None


@dataclass(frozen=True)
class SlopeEstimate:
    value_per_s: float | None
    valid: bool
    sample_count: int
    span_s: float
    r_squared: float | None
    residual_mad: float | None
    rejected_points: int = 0
    reason: str = ""


def _fit(x, y):
    if len(x) < 2 or len(x) != len(y):
        return None
    xm, ym = fmean(x), fmean(y)
    denom = sum((z - xm) ** 2 for z in x)
    if denom <= 0:
        return None
    slope = sum((xi - xm) * (yi - ym) for xi, yi in zip(x, y)) / denom
    return slope, ym - slope * xm


def robust_linear_slope(times, values, *, window_s: float, min_points: int, min_span_s: float) -> SlopeEstimate:
    pairs = []
    for t, y in zip(list(times), list(values)):
        ts, val = _seconds(t), _finite(y)
        if ts is not None and val is not None:
            pairs.append((ts, val))
    if not pairs:
        return SlopeEstimate(None, False, 0, 0.0, None, None, reason="no finite samples")
    pairs.sort()
    latest = pairs[-1][0]
    pairs = [p for p in pairs if latest - p[0] <= max(0.1, float(window_s))]
    if len(pairs) < max(2, int(min_points)):
        span = pairs[-1][0] - pairs[0][0] if len(pairs) > 1 else 0.0
        return SlopeEstimate(None, False, len(pairs), span, None, None, reason="insufficient points")
    t0 = pairs[0][0]
    x = [p[0] - t0 for p in pairs]
    y = [p[1] for p in pairs]
    span = x[-1] - x[0]
    if span < max(0.1, float(min_span_s)):
        return SlopeEstimate(None, False, len(x), span, None, None, reason="insufficient time span")
    fitted = _fit(x, y)
    if fitted is None:
        return SlopeEstimate(None, False, len(x), span, None, None, reason="singular time axis")
    slope, intercept = fitted
    residuals = [yi - (slope * xi + intercept) for xi, yi in zip(x, y)]
    med = median(residuals)
    mad = float(median([abs(r - med) for r in residuals]))
    keep = [True] * len(x)
    if mad > 1e-12:
        threshold = 4.0 * 1.4826 * mad
        keep = [abs(r - med) <= threshold for r in residuals]
    else:
        # A nearly perfect linear series plus one isolated spike has zero
        # median absolute deviation.  Use a scale-aware numerical floor so the
        # spike is still excluded without classifying floating-point round-off
        # as a physical outlier.
        numerical_floor = max(1e-9, max((abs(v) for v in y), default=1.0) * 1e-9)
        keep = [abs(r - med) <= numerical_floor for r in residuals]
    fx = [v for v, k in zip(x, keep) if k]
    fy = [v for v, k in zip(y, keep) if k]
    rejected = len(x) - len(fx)
    if len(fx) >= max(2, int(min_points)):
        refit = _fit(fx, fy)
        if refit:
            slope, intercept = refit
            x, y = fx, fy
    predicted = [slope * xi + intercept for xi in x]
    ym = fmean(y)
    ss_res = sum((yi - pi) ** 2 for yi, pi in zip(y, predicted))
    ss_tot = sum((yi - ym) ** 2 for yi in y)
    r2 = 1.0 if ss_tot <= 1e-20 and ss_res <= 1e-20 else (0.0 if ss_tot <= 1e-20 else 1.0 - ss_res / ss_tot)
    return SlopeEstimate(float(slope), True, len(x), float(max(x) - min(x)), float(max(-1.0, min(1.0, r2))), mad, rejected)


def robust_rate_from_thickness(times, thickness, *, window_s=45.0, min_points=20, min_span_s=20.0):
    return robust_linear_slope(times, thickness, window_s=window_s, min_points=min_points, min_span_s=min_span_s)


@dataclass(frozen=True)
class CalibrationRatioResult:
    ratio: float | None
    sample_target_a: float | None
    crossing_time_s: float | None
    ck1_relative_at_crossing_a: float | None
    fit_slope_ratio: float | None
    linearity_r2: float | None
    quality_pass: bool
    quality_message: str
    synchronized_fit_points: int
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ratio": self.ratio,
            "crossing_timestamp": (
                datetime.fromtimestamp(self.crossing_time_s).isoformat()
                if self.crossing_time_s is not None else None
            ),
            "sample_target_a": self.sample_target_a,
            "ck1_relative_at_crossing_a": self.ck1_relative_at_crossing_a,
            "fit_slope_ratio": self.fit_slope_ratio,
            "linearity_r2": self.linearity_r2,
            "quality_pass": self.quality_pass,
            "quality_message": self.quality_message,
            "synchronized_fit_points": self.synchronized_fit_points,
            "reason": self.reason,
        }


def _finite_pairs(times, values, *, start_s=None):
    pairs = []
    for stamp, value in zip(list(times), list(values)):
        ts, clean = _seconds(stamp), _finite(value)
        if ts is None or clean is None or (start_s is not None and ts < start_s):
            continue
        pairs.append((ts, clean))
    pairs.sort(key=lambda item: item[0])
    return pairs


def _interpolate_pairs(pairs, target_s):
    if not pairs or target_s < pairs[0][0] or target_s > pairs[-1][0]:
        return None
    for (t0, y0), (t1, y1) in zip(pairs, pairs[1:]):
        if t0 <= target_s <= t1:
            if t1 <= t0:
                return y1
            fraction = (target_s - t0) / (t1 - t0)
            return y0 + fraction * (y1 - y0)
    return pairs[-1][1]


def exact_calibration_ratio(
    *,
    sample_times,
    sample_thickness,
    ck1_times,
    ck1_thickness,
    sample_baseline_a,
    ck1_baseline_a,
    sample_start_time,
    ck1_start_time,
    target_sample_a,
    minimum_linearity_r2=0.985,
) -> CalibrationRatioResult:
    """Calculate a CK-1/sample ratio at the exact Sample-QMB target crossing.

    The Sample-QMB crossing is linearly interpolated between adjacent trusted
    samples. CK-1 thickness is then interpolated at the same physical time.
    A synchronized CK-1-versus-sample fit supplies a calibration-linearity R².
    """
    sample_base = _finite(sample_baseline_a)
    ck1_base = _finite(ck1_baseline_a)
    sample_start = _seconds(sample_start_time)
    ck1_start = _seconds(ck1_start_time)
    target = _finite(target_sample_a)
    if None in (sample_base, ck1_base, sample_start, ck1_start, target) or target <= 0:
        reason = "calibration baselines, timestamps or target are invalid"
        return CalibrationRatioResult(None, target, None, None, None, None, False, reason, 0, reason)

    sample_pairs_abs = _finite_pairs(sample_times, sample_thickness, start_s=sample_start)
    ck1_pairs_abs = _finite_pairs(ck1_times, ck1_thickness, start_s=ck1_start)
    sample_pairs = [(ts, value - sample_base) for ts, value in sample_pairs_abs]
    crossing = None
    for (t0, y0), (t1, y1) in zip(sample_pairs, sample_pairs[1:]):
        if y0 <= target <= y1 and y1 > y0 and t1 > t0:
            crossing = t0 + (target - y0) / (y1 - y0) * (t1 - t0)
            break
    if crossing is None:
        reason = "Sample QMB target crossing could not be interpolated"
        return CalibrationRatioResult(None, target, None, None, None, None, False, reason, 0, reason)

    ck1_absolute = _interpolate_pairs(ck1_pairs_abs, crossing)
    if ck1_absolute is None:
        reason = "CK-1 thickness is unavailable at the Sample-QMB crossing"
        return CalibrationRatioResult(None, target, crossing, None, None, None, False, reason, 0, reason)
    ck1_relative = ck1_absolute - ck1_base
    ratio = ck1_relative / target

    fit_sample, fit_ck1 = [], []
    for ts, sample_relative in sample_pairs:
        if ts > crossing:
            break
        ck1_at_ts = _interpolate_pairs(ck1_pairs_abs, ts)
        if ck1_at_ts is None:
            continue
        fit_sample.append(sample_relative)
        fit_ck1.append(ck1_at_ts - ck1_base)

    fit_slope = r2 = None
    if len(fit_sample) >= 5:
        fitted = _fit(fit_sample, fit_ck1)
        if fitted is not None:
            fit_slope, intercept = fitted
            predicted = [fit_slope * x + intercept for x in fit_sample]
            y_mean = fmean(fit_ck1)
            ss_res = sum((y - pred) ** 2 for y, pred in zip(fit_ck1, predicted))
            ss_tot = sum((y - y_mean) ** 2 for y in fit_ck1)
            r2 = 1.0 if ss_tot <= 1e-20 and ss_res <= 1e-20 else (0.0 if ss_tot <= 1e-20 else 1.0 - ss_res / ss_tot)
            r2 = float(max(-1.0, min(1.0, r2)))

    failures = []
    min_r2 = float(minimum_linearity_r2)
    if r2 is None:
        failures.append("insufficient synchronized QMB points for linearity assessment")
    elif r2 < min_r2:
        failures.append(f"linearity R²={r2:.5f} < {min_r2:.5f}")


    passed = not failures
    message = (
        "PASS: exact-crossing ratio and synchronized QMB linearity checks passed."
        if passed else "REVIEW / REPEAT RECOMMENDED: " + "; ".join(failures)
    )
    return CalibrationRatioResult(
        float(ratio), float(target), float(crossing), float(ck1_relative),
        None if fit_slope is None else float(fit_slope), r2,
        passed, message, len(fit_sample), "accepted" if passed else "; ".join(failures),
    )


@dataclass(frozen=True)
class QmbGuardConfig:
    """Plausibility limits for QMB data before it enters plots or control."""

    max_abs_rate_a_per_s: float = 10.0
    max_thickness_rate_a_per_s: float = 10.0
    min_allowed_thickness_jump_a: float = 5.0


@dataclass(frozen=True)
class QmbGuardDecision:
    accepted: bool
    value: float | None
    reason: str
    previous_value: float | None = None
    elapsed_s: float | None = None
    derived_rate_a_per_s: float | None = None


class QmbSignalGuard:
    """Reject non-finite, impossible-rate and impossible-thickness samples.

    A rejected thickness sample does not replace the last trusted reference.
    Consequently, a true QMB reset remains invalid until the operator restarts
    or re-zeros the phase, rather than silently creating a false deposition
    segment that could be consumed by feedback control.
    """

    def __init__(self, config: QmbGuardConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.last_thickness: float | None = None
        self.last_thickness_time_s: float | None = None

    def check_rate(self, value) -> QmbGuardDecision:
        clean = _finite(value)
        if clean is None:
            return QmbGuardDecision(False, None, "non-finite QMB rate")
        if abs(clean) > abs(float(self.config.max_abs_rate_a_per_s)):
            return QmbGuardDecision(
                False, clean,
                f"absolute QMB rate {clean:.6g} Å/s exceeds {abs(float(self.config.max_abs_rate_a_per_s)):.6g} Å/s",
            )
        return QmbGuardDecision(True, clean, "accepted")

    def check_thickness(self, value, timestamp) -> QmbGuardDecision:
        clean = _finite(value)
        ts = _seconds(timestamp)
        if clean is None or ts is None:
            return QmbGuardDecision(False, clean, "non-finite QMB thickness or timestamp")
        previous = self.last_thickness
        previous_ts = self.last_thickness_time_s
        if previous is None or previous_ts is None:
            self.last_thickness = clean
            self.last_thickness_time_s = ts
            return QmbGuardDecision(True, clean, "accepted first sample")
        elapsed = ts - previous_ts
        if elapsed <= 0:
            return QmbGuardDecision(False, clean, "non-increasing QMB timestamp", previous, elapsed)
        delta = clean - previous
        derived = delta / elapsed
        allowed_jump = max(
            abs(float(self.config.min_allowed_thickness_jump_a)),
            abs(float(self.config.max_thickness_rate_a_per_s)) * elapsed
            + abs(float(self.config.min_allowed_thickness_jump_a)),
        )
        if abs(delta) > allowed_jump:
            return QmbGuardDecision(
                False, clean,
                f"QMB thickness jump {delta:+.6g} Å in {elapsed:.3f} s is implausible",
                previous, elapsed, derived,
            )
        self.last_thickness = clean
        self.last_thickness_time_s = ts
        return QmbGuardDecision(True, clean, "accepted", previous, elapsed, derived)


DATA_QUALITY_FIELDS = (
    "timestamp", "device", "signal", "raw_value", "reason",
    "previous_value", "elapsed_s", "derived_rate_a_per_s",
)


class DataQualityEventLogger:
    """Append rejected sensor samples to a compact audit CSV."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=DATA_QUALITY_FIELDS).writeheader()

    def log(self, **values):
        row = {key: values.get(key, "") for key in DATA_QUALITY_FIELDS}
        with self.lock:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=DATA_QUALITY_FIELDS).writerow(row)


@dataclass(frozen=True)
class TemperaturePidConfig:
    kp: float
    ki: float
    kd: float
    deadband_c: float
    integral_limit_c_s: float
    integral_active_error_c: float
    derivative_tau_s: float
    max_up_slew_a_per_min: float
    max_down_slew_a_per_min: float
    min_current_a: float = 0.0
    max_current_a: float = 0.660


@dataclass(frozen=True)
class TemperaturePidDecision:
    delta_a: float
    raw_delta_a: float
    error_c: float
    p_a: float
    i_a: float
    d_a: float
    integral_c_s: float
    derivative_c_per_s: float
    inside_deadband: bool
    initialized: bool
    slew_limited: bool
    current_limited: bool
    integral_frozen: bool


class TemperaturePidController:
    """Incremental PID with derivative-on-measurement and complete anti-windup."""
    def __init__(self, config: TemperaturePidConfig):
        self.config = config
        self.reset()

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(v, hi))

    def reset(self, *, now_s=None, measurement_c=None, target_c=None, bumpless=True):
        self.last_time = float(now_s) if now_s is not None else None
        self.last_measurement = float(measurement_c) if measurement_c is not None else None
        self.filtered_derivative = 0.0
        self.integral = 0.0
        if bumpless and measurement_c is not None and target_c is not None and abs(self.config.ki) > 1e-20:
            err = float(target_c) - float(measurement_c)
            self.integral = self._clamp(-self.config.kp * err / self.config.ki, -abs(self.config.integral_limit_c_s), abs(self.config.integral_limit_c_s))

    def update(self, *, target_c, measurement_c, current_a, now_s, deadband_c=None, actuator_blocked=False):
        target_c, measurement_c, current_a, now_s = map(float, (target_c, measurement_c, current_a, now_s))
        error = target_c - measurement_c
        band = abs(self.config.deadband_c if deadband_c is None else float(deadband_c))
        if self.last_time is None or self.last_measurement is None:
            self.last_time, self.last_measurement = now_s, measurement_c
            return TemperaturePidDecision(0, 0, error, 0, 0, 0, self.integral, 0, abs(error) <= band, False, False, False, True)
        dt = max(0.1, now_s - self.last_time)
        raw_d = (measurement_c - self.last_measurement) / dt
        tau = max(0.0, self.config.derivative_tau_s)
        alpha = 1.0 if tau <= 0 else dt / (tau + dt)
        self.filtered_derivative += alpha * (raw_d - self.filtered_derivative)
        inside = abs(error) <= band
        old_i = self.integral
        candidate = old_i
        integral_frozen = bool(actuator_blocked)
        if inside:
            candidate *= 0.90
        elif abs(error) <= abs(self.config.integral_active_error_c) and not actuator_blocked:
            candidate += error * dt
        else:
            integral_frozen = True
        candidate = self._clamp(candidate, -abs(self.config.integral_limit_c_s), abs(self.config.integral_limit_c_s))
        p = 0.0 if inside else self.config.kp * error
        i_candidate = self.config.ki * candidate
        d = -self.config.kd * self.filtered_derivative
        raw = 0.0 if inside else p + i_candidate + d
        up = abs(self.config.max_up_slew_a_per_min) * dt / 60.0
        down = abs(self.config.max_down_slew_a_per_min) * dt / 60.0
        delta = self._clamp(raw, -down, up)
        slew_limited = not math.isclose(delta, raw, abs_tol=1e-15)
        requested = self._clamp(current_a + delta, self.config.min_current_a, self.config.max_current_a)
        actual = requested - current_a
        current_limited = not math.isclose(actual, delta, abs_tol=1e-15)
        pushes = actuator_blocked or (slew_limited and raw * error > 0) or (current_limited and actual * error >= 0)
        if not pushes:
            self.integral = candidate
        else:
            self.integral = old_i
            integral_frozen = True
        self.last_time, self.last_measurement = now_s, measurement_c
        return TemperaturePidDecision(actual, raw, error, p, self.config.ki * self.integral, d, self.integral, self.filtered_derivative, inside, True, slew_limited, current_limited, integral_frozen)


@dataclass(frozen=True)
class CascadeConfig:
    kp_c_per_rate: float
    ki_c_per_thickness: float
    deadband_rate: float
    integral_limit_thickness: float
    max_up_c_per_min: float
    max_down_c_per_min: float
    trim_limit_c: float
    settling_s: float
    trend_hold_threshold_per_s: float
    temperature_guard_band_c: float = 0.0
    max_step_c: float = math.inf


@dataclass(frozen=True)
class CascadeDecision:
    target_c: float
    delta_c: float
    raw_delta_c: float
    error_rate: float
    p_c: float
    i_c: float
    integral: float
    inside_deadband: bool
    settling: bool
    trend_hold: bool
    limited: bool
    integral_frozen: bool


class CascadeRateController:
    """Slow outer PI loop: rate changes a temperature target, never current."""
    def __init__(self, config: CascadeConfig):
        self.config = config
        self.reset(base_target_c=0.0)

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(v, hi))

    def reset(self, *, base_target_c, current_target_c=None, now_s=None):
        self.base_target = float(base_target_c)
        self.target = float(base_target_c if current_target_c is None else current_target_c)
        self.integral = 0.0
        self.last_time = float(now_s) if now_s is not None else None
        self.last_change = float(now_s) if now_s is not None else None

    def update(self, *, target_rate, measured_rate, rate_trend_per_s, base_target_c, max_temp_c, now_s, deadband_rate=None, freeze=False):
        target_rate, measured_rate, base_target_c, max_temp_c, now_s = map(float, (target_rate, measured_rate, base_target_c, max_temp_c, now_s))
        if not math.isclose(base_target_c, self.base_target, abs_tol=1e-12):
            # A live guide-target edit must not step the active thermal target.
            # Keep the present target and let the normal slew limits move it
            # toward the new admissible range on subsequent outer-loop updates.
            self.base_target = base_target_c
        error = target_rate - measured_rate
        band = abs(self.config.deadband_rate if deadband_rate is None else float(deadband_rate))
        initialized = self.last_time is not None
        dt = max(0.1, now_s - self.last_time) if initialized else 0.0
        settling = self.last_change is not None and now_s - self.last_change < max(0.0, self.config.settling_s)
        trend = _finite(rate_trend_per_s)
        trend_hold = False
        threshold = abs(self.config.trend_hold_threshold_per_s)
        if trend is not None and threshold > 0:
            trend_hold = (error > 0 and trend >= threshold) or (error < 0 and trend <= -threshold)
        inside = abs(error) <= band
        old_i = self.integral
        candidate = old_i
        frozen = bool(freeze or settling or trend_hold)
        if inside:
            candidate *= 0.90
        elif initialized and not frozen:
            candidate += error * dt
        candidate = self._clamp(candidate, -abs(self.config.integral_limit_thickness), abs(self.config.integral_limit_thickness))
        p = 0.0 if inside else self.config.kp_c_per_rate * error
        i_candidate = self.config.ki_c_per_thickness * candidate
        raw = 0.0 if inside or settling or trend_hold else p + i_candidate
        # Accumulate the slew allowance from the last *applied target action*,
        # not from the last evaluation.  During a multi-minute settling hold,
        # evaluation continues for logging and signal checks; resetting the slew
        # clock on every held evaluation would unintentionally reduce a
        # 0.20 °C/min limit to only 0.20 °C every four minutes. A separate
        # per-action cap keeps each
        # post-settling correction conservative.
        if initialized:
            reference = self.last_change if self.last_change is not None else self.last_time
            slew_elapsed = max(0.1, now_s - float(reference))
        else:
            slew_elapsed = 0.0
        up = abs(self.config.max_up_c_per_min) * slew_elapsed / 60.0 if initialized else 0.0
        down = abs(self.config.max_down_c_per_min) * slew_elapsed / 60.0 if initialized else 0.0
        step_cap = abs(float(self.config.max_step_c))
        if math.isfinite(step_cap):
            up = min(up, step_cap)
            down = min(down, step_cap)
        delta = self._clamp(raw, -down, up)
        # Progressively soften only upward target movement near the absolute
        # rate-control temperature ceiling. Downward corrections remain fully
        # available, and the independent watchdog remains separate.
        guard = max(0.0, float(self.config.temperature_guard_band_c))
        if delta > 0 and guard > 0:
            remaining = max_temp_c - self.target
            if remaining <= 0:
                delta = 0.0
            elif remaining < guard:
                delta *= self._clamp(remaining / guard, 0.0, 1.0)
        lower = base_target_c - abs(self.config.trim_limit_c)
        upper = min(base_target_c + abs(self.config.trim_limit_c), max_temp_c)
        candidate_target = self.target + delta
        if self.target < lower:
            # Never jump directly to a bound after a low-temperature handover.
            # Only permit a slew-limited move upward toward the admissible range.
            requested = min(lower, max(self.target, candidate_target))
        elif self.target > upper:
            # Symmetric handling for a target that is already above the range.
            requested = max(upper, min(self.target, candidate_target))
        else:
            requested = self._clamp(candidate_target, lower, upper)
        actual = requested - self.target
        limited = not math.isclose(actual, raw, abs_tol=1e-12)
        if not frozen and not (limited and raw * error > 0):
            self.integral = candidate
        else:
            frozen = True
            self.integral = old_i
        if abs(actual) > 1e-12:
            self.target = requested
            self.last_change = now_s
        self.last_time = now_s
        return CascadeDecision(self.target, actual, raw, error, p, self.config.ki_c_per_thickness * self.integral, self.integral, inside, settling, trend_hold, limited, frozen)


class StableBandTracker:
    def __init__(self, tolerance, duration_s):
        self.tolerance = abs(float(tolerance)); self.duration_s = max(0.0, float(duration_s)); self.reset()
    def reset(self):
        self.since = None
    def update(self, value, target, now_s):
        value = _finite(value); target = float(target); now_s = float(now_s)
        if value is None or abs(value - target) > self.tolerance:
            self.since = None
        elif self.since is None:
            self.since = now_s
        return self.since is not None and now_s - self.since >= self.duration_s
    def elapsed(self, now_s):
        return 0.0 if self.since is None else max(0.0, float(now_s) - self.since)


class StableConditionTracker:
    """Require an arbitrary boolean condition to remain true continuously."""

    def __init__(self, duration_s):
        self.duration_s = max(0.0, float(duration_s))
        self.reset()

    def reset(self):
        self.since = None

    def update(self, condition, now_s):
        now_s = float(now_s)
        if not bool(condition):
            self.since = None
        elif self.since is None:
            self.since = now_s
        return self.since is not None and now_s - self.since >= self.duration_s

    def elapsed(self, now_s):
        return 0.0 if self.since is None else max(0.0, float(now_s) - self.since)


CONTROL_FIELDS = (
    "timestamp", "monotonic_s", "phase", "mode", "active_controller", "molecule_profile",
    "raw_temperature_c", "control_temperature_c", "base_temperature_target_c", "active_temperature_target_c",
    "raw_qmb_rate_a_per_s", "estimated_rate_a_per_s", "rate_fit_r_squared", "rate_fit_span_s", "rate_fit_points",
    "rate_target_a_per_s", "error", "p_term", "i_term", "d_term", "requested_delta", "applied_delta",
    "current_before_a", "current_after_a", "inside_deadband", "integral_frozen", "slew_or_step_limited",
    "current_or_target_limited", "settling", "trend_hold", "temperature_limited", "signal_valid",
    "temperature_slope_c_per_min", "rate_trend_a_per_s2", "inner_loop_ready",
    "inner_ready_elapsed_s", "thermal_response_pending", "last_outer_action_age_s",
    "outer_freeze_reason", "reason",
)


class ControlDecisionLogger:
    def __init__(self, path):
        self.path = str(path); self.lock = threading.Lock(); os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CONTROL_FIELDS).writeheader()
    def log(self, **values):
        row = {k: values.get(k, "") for k in CONTROL_FIELDS}
        with self.lock:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=CONTROL_FIELDS).writerow(row)
