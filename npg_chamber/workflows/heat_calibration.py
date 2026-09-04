"""Packaged entry point for Heat up + Calibration.

This module delegates to the authoritative phase script stored in
``npg_chamber.phase_scripts`` so ``npg-chamber --run heat`` uses the exact
source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and reusable hardware helpers.
"""

from __future__ import annotations

from npg_chamber.workflows.runner import run_workflow


def run_heat_calibration() -> int:
    """Run the exact packaged Heat up + Calibration script."""

    return run_workflow("heat")


def main() -> int:
    return run_heat_calibration()


if __name__ == "__main__":
    raise SystemExit(main())
