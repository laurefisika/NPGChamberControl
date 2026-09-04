"""Path and filename helpers."""

from __future__ import annotations

import os
import re
from importlib import resources
from pathlib import Path


DATA_SAMPLES_FOLDER = "Data Samples"

PHASE_DATA_FOLDERS: dict[str, str] = {
    "heat": "Heat up + Calibration Data",
    "sputter": "Sputtering-Annealing Data",
    "dpdbba": "DP-DBBA Evaporation Data",
    "anneal": "NPG Annealing Data",
}

PHASE_RUN_LABELS: dict[str, str] = {
    "heat": "Heat up + Calibration",
    "sputter": "Sputtering-Annealing",
    "dpdbba": "DP-DBBA Evaporation",
    "anneal": "NPG Annealings",
}


def project_root() -> Path:
    """Return the project root when running from the source tree."""

    return Path(__file__).resolve().parents[2]


def phase_script_dir() -> Path:
    """Return the authoritative folder containing the four phase scripts."""

    packaged_scripts = resources.files("npg_chamber.phase_scripts")
    return Path(str(packaged_scripts))


def data_samples_dir() -> Path:
    """Return the data folder used by all workflow runs.

    In source/editable mode, data are saved in the project root. In an installed
    wheel, the package lives in site-packages, so data are saved in the current
    working directory instead.
    """

    root = project_root()
    if not ((root / "pyproject.toml").exists() and (root / "npg_chamber").exists()):
        root = Path.cwd()

    path = root / DATA_SAMPLES_FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def phase_data_dir(workflow_key: str) -> Path:
    """Return the dedicated data parent folder for one workflow."""

    try:
        folder_name = PHASE_DATA_FOLDERS[workflow_key]
    except KeyError as exc:
        valid = ", ".join(sorted(PHASE_DATA_FOLDERS))
        raise ValueError(f"Unknown workflow {workflow_key!r}. Valid values: {valid}") from exc

    path = data_samples_dir() / folder_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_run_name(run_name: str, *, fallback: str = "Run") -> str:
    """Return a compact run name that is safe in a Windows folder name."""

    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(run_name)).strip()
    safe = re.sub(r"\s+", " ", safe).rstrip(" .")
    return safe if safe and safe not in {".", ".."} else fallback


def phase_run_folder_prefix(workflow_key: str, run_name: str) -> str:
    """Return the shared phase/sample prefix used by every run-data folder."""

    try:
        phase_label = PHASE_RUN_LABELS[workflow_key]
    except KeyError as exc:
        valid = ", ".join(sorted(PHASE_RUN_LABELS))
        raise ValueError(f"Unknown workflow {workflow_key!r}. Valid values: {valid}") from exc
    return f"{phase_label} {safe_run_name(run_name)} data"


def create_numbered_run_dir(
    workflow_key: str,
    run_name: str,
    *,
    parent: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically reserve the next ``<phase> <sample> data NN`` directory.

    Numbering always starts at ``00`` and uses at least two digits. Creating the
    directory here prevents two near-simultaneous launches from selecting the
    same run folder.
    """

    root = Path(parent) if parent is not None else phase_data_dir(workflow_key)
    root.mkdir(parents=True, exist_ok=True)
    prefix = phase_run_folder_prefix(workflow_key, run_name)

    counter = 0
    while True:
        candidate = root / f"{prefix} {counter:02d}"
        try:
            candidate.mkdir()
        except FileExistsError:
            counter += 1
            continue
        return candidate
