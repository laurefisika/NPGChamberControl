"""Packaged entry point for NPG Annealings.

This module delegates to the authoritative phase script stored in
``npg_chamber.phase_scripts`` so ``npg-chamber --run anneal`` uses the exact
source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and reusable hardware helpers.
"""

from __future__ import annotations

from npg_chamber.workflows.runner import run_workflow


def run_npg_annealing() -> int:
    """Run the exact packaged NPG Annealings script."""

    return run_workflow("anneal")


def main() -> int:
    return run_npg_annealing()


if __name__ == "__main__":
    raise SystemExit(main())
