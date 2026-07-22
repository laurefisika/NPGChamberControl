"""Path and filename helpers."""

from __future__ import annotations

from importlib import resources
from pathlib import Path


DATA_SAMPLES_FOLDER = "Data Samples"

PHASE_DATA_FOLDERS: dict[str, str] = {
    "heat": "Heat up + Calibration Data",
    "sputter": "Sputtering-Annealing Data",
    "dpdbba": "DP-DBBA Evaporation Data",
    "anneal": "NPG Annealing Data",
}


def project_root() -> Path:
    """Return the project root when running from the source tree."""

    return Path(__file__).resolve().parents[2]


def legacy_dir() -> Path:
    """Return the authoritative folder containing the four phase scripts."""

    packaged_scripts = resources.files("npg_chamber.legacy_scripts")
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


def ensure_all_data_sample_folders() -> None:
    """Create all standard data folders."""

    for key in PHASE_DATA_FOLDERS:
        phase_data_dir(key)
