#!/usr/bin/env python3
"""Run the safe, no-hardware general checks for the package."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_step(label: str, command: list[str]) -> None:
    print(f"\n=== {label} ===")
    print(" ".join(command))
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    python = sys.executable
    run_step("pytest", [python, "-m", "pytest", "-q", "developer_tests"])
    run_step("compileall", [python, "-m", "compileall", "-q", "npg_chamber", "developer_tests", "diagnostic_tools", "maintenance_tools"])
    run_step("launcher --list", [python, "-m", "npg_chamber", "--list"])
    run_step("launcher --version", [python, "-m", "npg_chamber", "--version"])
    print("\nAll safe general checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
