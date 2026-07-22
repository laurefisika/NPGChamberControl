"""Packaged entry point for Sputtering-Annealing.

This module deliberately launches the frozen script stored in
``npg_chamber.legacy_scripts`` so ``npg-chamber --run sputter`` uses the exact
final source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and future refactoring, while this entry
point preserves the current experimental scripts byte-for-byte.
"""

from __future__ import annotations

from npg_chamber.workflows.legacy_runner import run_legacy_workflow


def run_sputter_anneal() -> int:
    """Run the exact packaged Sputtering-Annealing script."""

    return run_legacy_workflow("sputter")


def main() -> int:
    return run_sputter_anneal()


if __name__ == "__main__":
    raise SystemExit(main())
