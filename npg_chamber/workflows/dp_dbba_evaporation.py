"""Packaged entry point for DP-DBBA Evaporation.

This module delegates to the authoritative phase script stored in
``npg_chamber.phase_scripts`` so ``npg-chamber --run dpdbba`` uses the exact
source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and reusable hardware helpers.
"""

from __future__ import annotations

from npg_chamber.workflows.runner import run_workflow


def run_dp_dbba_evaporation() -> int:
    """Run the exact packaged DP-DBBA Evaporation script."""

    return run_workflow("dpdbba")


def main() -> int:
    return run_dp_dbba_evaporation()


if __name__ == "__main__":
    raise SystemExit(main())
