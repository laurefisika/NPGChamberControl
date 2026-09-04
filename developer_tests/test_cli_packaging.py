from __future__ import annotations

import importlib.resources as resources
import subprocess
import sys
from pathlib import Path

import npg_chamber
from npg_chamber.cli import main


def test_cli_version_outputs_package_version(capsys):
    exit_code = main(["--version"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == npg_chamber.__version__


def test_cli_list_outputs_all_workflows(capsys):
    exit_code = main(["--list"])
    captured = capsys.readouterr()
    assert exit_code == 0
    for key in ["heat", "sputter", "dpdbba", "anneal"]:
        assert key in captured.out


def test_python_module_launcher_list_works():
    completed = subprocess.run(
        [sys.executable, "-m", "npg_chamber", "--list"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "Available workflows" in completed.stdout


def test_pyproject_version_matches_package_version():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover for Python < 3.11
        import tomli as tomllib  # type: ignore

    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == npg_chamber.__version__


def test_phase_scripts_are_packaged_resources():
    phase_root = resources.files("npg_chamber.phase_scripts")
    expected = [
        "01_heat_up_calibration.py",
        "02_sputtering_annealing.py",
        "03_dp_dbba_evaporation.py",
        "04_npg_annealings.py",
    ]
    for name in expected:
        assert (phase_root / name).is_file()


def test_cli_help_exposes_direct_phase_execution():
    completed = subprocess.run(
        [sys.executable, "-m", "npg_chamber", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--run" in completed.stdout
    assert "--run-legacy" not in completed.stdout


def test_cli_help_mentions_text_menu():
    completed = subprocess.run(
        [sys.executable, "-m", "npg_chamber", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--text-menu" in completed.stdout


def test_gui_launcher_module_imports():
    import npg_chamber.gui_launcher as gui_launcher

    assert [phase.key for phase in gui_launcher.PHASES] == ["heat", "sputter", "dpdbba", "anneal"]
    assert gui_launcher.NEXT_PHASE["heat"] == "sputter"


def test_explanation_pdfs_are_packaged_resources():
    explanation_root = resources.files("npg_chamber.script_explanations")
    expected = [
        "01_heat_up_calibration_explanation.pdf",
        "02_sputtering_annealing_explanation.pdf",
        "03_dp_dbba_evaporation_explanation.pdf",
        "04_npg_annealings_explanation.pdf",
    ]
    for name in expected:
        assert (explanation_root / name).is_file()
