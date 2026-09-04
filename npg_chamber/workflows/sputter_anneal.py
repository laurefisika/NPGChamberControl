"""Packaged entry point for Sputtering-Annealing.

This module delegates to the authoritative phase script stored in
``npg_chamber.phase_scripts`` so ``npg-chamber --run sputter`` uses the exact
source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and reusable hardware helpers.
"""

from __future__ import annotations

from npg_chamber.workflows.runner import run_workflow


def run_sputter_anneal() -> int:
    """Run the exact packaged Sputtering-Annealing script."""

    return run_workflow("sputter")


def main() -> int:
    return run_sputter_anneal()


if __name__ == "__main__":
    raise SystemExit(main())
