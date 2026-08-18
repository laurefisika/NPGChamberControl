#!/usr/bin/env python3
"""Summarize a Phase 01/03 professional control-decision CSV.

The tool is intentionally offline and read-only.  It never opens a COM port and
never sends a hardware command.  Use it after a supervised run to compare
normal and fresh-post-refill behaviour with objective control metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Iterable


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def values(rows: Iterable[dict], key: str) -> list[float]:
    result = []
    for row in rows:
        value = finite(row.get(key))
        if value is not None:
            result.append(value)
    return result


def fraction(rows: list[dict], key: str) -> float:
    if not rows:
        return 0.0
    true_values = {"1", "true", "yes", "on", "True"}
    return sum(str(row.get(key, "")).strip() in true_values for row in rows) / len(rows)


def summarize(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("The control-decision CSV contains no data rows.")

    estimated_rate = values(rows, "estimated_rate_a_per_s")
    target_rate = values(rows, "rate_target_a_per_s")
    temperature = values(rows, "control_temperature_c")
    active_target = values(rows, "active_temperature_target_c")
    current = values(rows, "current_after_a")
    errors = values(rows, "error")
    applied = values(rows, "applied_delta")

    rate_errors = []
    for row in rows:
        measured = finite(row.get("estimated_rate_a_per_s"))
        target = finite(row.get("rate_target_a_per_s"))
        if measured is not None and target is not None:
            rate_errors.append(measured - target)

    modes: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        mode = row.get("mode", "") or "unknown"
        modes[mode] = modes.get(mode, 0) + 1
        reason = row.get("reason", "") or "unspecified"
        reasons[reason] = reasons.get(reason, 0) + 1

    def span(data):
        return None if not data else max(data) - min(data)

    summary = {
        "source": str(path),
        "rows": len(rows),
        "molecule_profiles": sorted({row.get("molecule_profile", "") for row in rows if row.get("molecule_profile", "")}),
        "modes": modes,
        "rate": {
            "samples": len(estimated_rate),
            "mean_a_per_s": None if not estimated_rate else fmean(estimated_rate),
            "maximum_a_per_s": None if not estimated_rate else max(estimated_rate),
            "mean_absolute_error_a_per_s": None if not rate_errors else fmean(abs(x) for x in rate_errors),
            "rms_error_a_per_s": None if not rate_errors else math.sqrt(fmean(x * x for x in rate_errors)),
        },
        "temperature": {
            "samples": len(temperature),
            "minimum_c": None if not temperature else min(temperature),
            "maximum_c": None if not temperature else max(temperature),
            "peak_to_peak_c": span(temperature),
            "active_target_peak_to_peak_c": span(active_target),
        },
        "current": {
            "samples": len(current),
            "minimum_a": None if not current else min(current),
            "maximum_a": None if not current else max(current),
            "peak_to_peak_a": span(current),
            "total_absolute_commanded_change_a": sum(abs(x) for x in applied),
        },
        "controller_activity": {
            "inside_deadband_fraction": fraction(rows, "inside_deadband"),
            "integral_frozen_fraction": fraction(rows, "integral_frozen"),
            "slew_or_step_limited_fraction": fraction(rows, "slew_or_step_limited"),
            "current_or_target_limited_fraction": fraction(rows, "current_or_target_limited"),
            "settling_fraction": fraction(rows, "settling"),
            "trend_hold_fraction": fraction(rows, "trend_hold"),
            "temperature_limited_fraction": fraction(rows, "temperature_limited"),
            "invalid_signal_fraction": 1.0 - fraction(rows, "signal_valid"),
        },
        "top_reasons": sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:12],
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="Phase 01/03 *_control_decisions.csv file")
    parser.add_argument("--json", dest="json_path", type=Path, help="Optional JSON output path")
    args = parser.parse_args()

    summary = summarize(args.csv_path)
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
