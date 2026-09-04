"""Command-line launcher for the NPG chamber workflows."""

from __future__ import annotations

import argparse
import sys

from npg_chamber import __version__
from npg_chamber.common.logging import banner, init_colors
from npg_chamber.workflows.runner import (
    WORKFLOWS,
    list_workflows,
    run_workflow,
)


MENU_OPTIONS = {
    "1": "heat",
    "2": "sputter",
    "3": "dpdbba",
    "4": "anneal",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NPG synthesis chamber controller")
    parser.add_argument(
        "--run",
        choices=sorted(WORKFLOWS),
        help="Run one packaged phase directly without showing the menu.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available workflows and exit.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show package version and exit.",
    )
    parser.add_argument(
        "--text-menu",
        action="store_true",
        help="Use the old text menu instead of the graphical launcher.",
    )
    return parser


def choose_from_menu() -> str | None:
    banner("NPG Chamber Controller")
    print("1 - Heat up + Calibration")
    print("2 - Sputtering-Annealing")
    print("3 - DP-DBBA Evaporation")
    print("4 - NPG Annealings")
    print("q - Quit")
    choice = input("\nChoose workflow: ").strip().lower()
    if choice in {"q", "quit", "exit"}:
        return None
    return MENU_OPTIONS.get(choice)


def main(argv: list[str] | None = None) -> int:
    init_colors()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.list:
        print(list_workflows())
        return 0

    if args.run:
        workflow_key = args.run
    elif args.text_menu:
        workflow_key = choose_from_menu()
    else:
        try:
            from npg_chamber.gui_launcher import launch_gui

            return launch_gui()
        except Exception as exc:
            print(f"Could not open graphical launcher: {exc}")
            print("Falling back to text menu. Use --text-menu to force this mode.")
            workflow_key = choose_from_menu()
    if workflow_key is None:
        print("Bye.")
        return 0

    if workflow_key not in WORKFLOWS:
        print("Invalid option. Use --list to see available workflows.")
        return 2

    if workflow_key == "heat":
        from npg_chamber.workflows.heat_calibration import run_heat_calibration

        return run_heat_calibration()

    if workflow_key == "sputter":
        from npg_chamber.workflows.sputter_anneal import run_sputter_anneal

        return run_sputter_anneal()

    if workflow_key == "dpdbba":
        from npg_chamber.workflows.dp_dbba_evaporation import run_dp_dbba_evaporation

        return run_dp_dbba_evaporation()

    if workflow_key == "anneal":
        from npg_chamber.workflows.npg_annealing import run_npg_annealing

        return run_npg_annealing()

    return run_workflow(workflow_key)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
