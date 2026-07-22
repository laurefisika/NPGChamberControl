"""Packaged entry point for NPG Annealings.

This module deliberately launches the frozen script stored in
``npg_chamber.legacy_scripts`` so ``npg-chamber --run anneal`` uses the exact
final source file supplied for packaging. The shared device modules remain in
``npg_chamber.devices`` for diagnostics and future refactoring, while this entry
point preserves the current experimental scripts byte-for-byte.
"""

from __future__ import annotations

from npg_chamber.workflows.legacy_runner import run_legacy_workflow


def run_npg_annealing() -> int:
    """Run the exact packaged NPG Annealings script."""

    return run_legacy_workflow("anneal")


def main() -> int:
    return run_npg_annealing()


if __name__ == "__main__":
    raise SystemExit(main())
