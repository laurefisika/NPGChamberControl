"""Local source/runtime verification for the Windows launcher.

This module intentionally performs text-level checks instead of importing the
legacy phase scripts, because importing those scripts may initialize hardware
state. It is safe to run before the graphical launcher opens.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from npg_chamber import __build__, __version__


@dataclass(frozen=True)
class Check:
    label: str
    path: str
    markers: tuple[str, ...]


CHECKS: tuple[Check, ...] = (
    Check(
        "Phase 01 control hotfix",
        "npg_chamber/legacy_scripts/01_heat_up_calibration_legacy.py",
        (
            "TEMP_SLOPE_WINDOW_S = 45.0",
            "TEMP_SLOPE_MIN_SPAN_S = 20.0",
        ),
    ),
    Check(
        "Phase 01 minimal shutter gate",
        "npg_chamber/legacy_scripts/01_heat_up_calibration_legacy.py",
        (
            "and float(ck1_temp) >= float(target_temp)",
            "and float(ck1_rate_avg) >= float(target_rate)",
            "Controller bands, rate trends, temperature slopes",
        ),
    ),
    Check(
        "Phase 03 minimal shutter gate",
        "npg_chamber/legacy_scripts/03_dp_dbba_evaporation_legacy.py",
        (
            "if not oven_ready_for_evaporation():",
            "and float(ck1_temp) >= float(target_temp)",
            "and float(ck1_rate_avg) >= float(target_rate)",
        ),
    ),
    Check(
        "Phase 01/03 readiness cleanup",
        "npg_chamber/config/run_parameters.py",
        (
            "CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN",
            "This is controller-internal and never gates shutter opening.",
        ),
    ),
    Check(
        "Phase 01 local Abort / Finish semantics",
        "npg_chamber/legacy_scripts/01_heat_up_calibration_legacy.py",
        (
            "GUI Abort button - immediate phase stop",
            "Safety action first. Do not delay OUTPUT OFF behind snapshots or file I/O.",
            "Entering RAMP_DOWN; Keysight output will switch OFF after reaching 0 A.",
        ),
    ),
    Check(
        "Phase 03 local Abort / Finish handoff semantics",
        "npg_chamber/legacy_scripts/03_dp_dbba_evaporation_legacy.py",
        (
            "GUI Abort button - immediate phase stop",
            "Keysight is already OFF, but Oven PID 0.0 °C could not be",
            "Set current has been returned to base current = {target_current:.3f} A.",
        ),
    ),
    Check(
        "Phase 03 thickness-ratio confirmation",
        "npg_chamber/gui_launcher.py",
        (
            "Do you agree with the thickness ratio obtained?",
            "No: modify the ratio before starting Phase 03.",
            "def _ask_manual_thickness_ratio",
        ),
    ),
    Check(
        "Phase 01/03 Qt dashboard",
        "npg_chamber/common/qt_phase_dashboard.py",
        (
            "apply_mode_button.clicked.connect(self._apply_feedback_mode)",
            "def _apply_adaptive_live_fit",
            'widget.getAxis("left").enableAutoSIPrefix(False)',
            'widget.getAxis("left").setScale(1.0)',
            'class RawValueAxis(pg.AxisItem)',
            '"Editable targets and controller", settings_tabs, expanded=True',
            'def apply_mode_with_live_targets()',
            'QRegularExpressionValidator(decimal_pattern, edit)',
            'def parse_locale_flexible_float',
            'def _mark_target_edit_dirty',
            'def _set_target_if_clean',
            'self._acknowledge_pending_targets(targets)',
            "splitter.setSizes([1310, 540])",
        ),
    ),
    Check(
        "Automation parameter UX",
        "npg_chamber/gui_launcher.py",
        (
            '"DEFAULT_RAMP_UP_MODE", "KEYSIGHT_BASE_WORK_CURRENT_A", "KEYSIGHT_STEP_A"',
            '"RAMPDOWN_STEP_A"',
            '"RAMPDOWN_STEP_PERIOD_S"',
        ),
    ),
    Check(
        "Phase 02 COSCON activation recovery",
        "npg_chamber/legacy_scripts/02_sputtering_annealing_legacy.py",
        (
            "coscon_activation_overload_retries: int = 1",
            "coscon_activation_recovery_wait_s: float = 8.0",
            "def _is_recoverable_activation_overload",
            "def _recover_activation_overload",
            "COSCON HV-Module Energy Overload repeated during activation",
        ),
    ),
    Check(
        "Phase 04 automatic finalization",
        "npg_chamber/legacy_scripts/04_npg_annealings_legacy.py",
        (
            "POST_COOLDOWN_WAIT_S = 10 * 60",
            "AUTO_CLOSE_WHEN_LAUNCHED_FROM_UNIFIED",
        ),
    ),
)


def project_root_from_package() -> Path:
    return Path(__file__).resolve().parents[1]


def verify(expected_build: str | None = None) -> bool:
    root = project_root_from_package()
    ok = True

    print(f"Verifying NPG Chamber source: v{__version__} ({__build__})")
    print(f"Active source folder: {root}")

    if expected_build and __build__ != expected_build:
        print(f"[FAIL] Build identity: expected {expected_build}, found {__build__}")
        ok = False
    else:
        print(f"[OK]   Build identity: {__build__}")

    cwd = Path.cwd().resolve()
    if cwd != root:
        print(f"[FAIL] Active project link: launcher folder is {cwd}")
        print(f"       but Python imports npg_chamber from {root}")
        ok = False
    else:
        print("[OK]   Active project link")

    for check in CHECKS:
        path = root / check.path
        if not path.is_file():
            print(f"[FAIL] {check.label}: missing {check.path}")
            ok = False
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[FAIL] {check.label}: cannot read {check.path}: {exc}")
            ok = False
            continue
        missing = [marker for marker in check.markers if marker not in text]
        if missing:
            print(f"[FAIL] {check.label}: {check.path}")
            for marker in missing:
                print(f"       missing marker: {marker}")
            ok = False
        else:
            print(f"[OK]   {check.label}")

    if ok:
        print("Source verification passed.")
    else:
        print("Source verification FAILED. Do not start a chamber phase from this mixed installation.")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the active NPG Chamber source tree.")
    parser.add_argument("--expected-build", default=None)
    args = parser.parse_args(argv)
    return 0 if verify(args.expected_build) else 1


if __name__ == "__main__":
    raise SystemExit(main())
