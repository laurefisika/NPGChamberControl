"""Packaged entry point for Heat up + Calibration.

This module deliberately launches the frozen script stored in
``npg_chamber.legacy_scripts`` so ``npg-chamber --run heat`` uses the exact
final source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and future refactoring, while this entry
point preserves the current experimental scripts byte-for-byte.
"""

from __future__ import annotations

from npg_chamber.workflows.legacy_runner import run_legacy_workflow


def run_heat_calibration() -> int:
    """Run the exact packaged Heat up + Calibration script."""

    return run_legacy_workflow("heat")


def main() -> int:
    return run_heat_calibration()


if __name__ == "__main__":
    raise SystemExit(main())
