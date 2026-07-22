"""Packaged entry point for DP-DBBA Evaporation.

This module deliberately launches the frozen script stored in
``npg_chamber.legacy_scripts`` so ``npg-chamber --run dpdbba`` uses the exact
final source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and future refactoring, while this entry
point preserves the current experimental scripts byte-for-byte.
"""

from __future__ import annotations

from npg_chamber.workflows.legacy_runner import run_legacy_workflow


def run_dp_dbba_evaporation() -> int:
    """Run the exact packaged DP-DBBA Evaporation script."""

    return run_legacy_workflow("dpdbba")


def main() -> int:
    return run_dp_dbba_evaporation()


if __name__ == "__main__":
    raise SystemExit(main())
