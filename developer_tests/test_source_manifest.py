from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_runtime_and_original_backup_files_match_manifest() -> None:
    manifest = json.loads((PROJECT_ROOT / "SOURCE_CODE_MANIFEST.json").read_text(encoding="utf-8"))
    for entry in manifest:
        resource_path = PROJECT_ROOT / entry["packaged_resource"]
        backup_path = PROJECT_ROOT / entry["backup_resource"]

        assert resource_path.is_file()
        assert backup_path.is_file()

        assert sha256(resource_path) == entry["sha256"]
        assert resource_path.stat().st_size == entry["size_bytes"]
        assert sha256(backup_path) == entry["backup_sha256"]
        assert backup_path.stat().st_size == entry["backup_size_bytes"]
        assert backup_path.read_bytes() == resource_path.read_bytes()


def test_original_backup_contains_exactly_four_phase_scripts() -> None:
    backup_dir = PROJECT_ROOT / "original_scripts_backup"
    expected = {
        "01_heat_up_calibration_legacy.py",
        "02_sputtering_annealing_legacy.py",
        "03_dp_dbba_evaporation_legacy.py",
        "04_npg_annealings_legacy.py",
    }
    assert backup_dir.is_dir()
    assert {path.name for path in backup_dir.glob("*.py")} == expected


def test_workflow_entry_points_delegate_only_to_packaged_runtime_scripts() -> None:
    workflow_files = [
        "heat_calibration.py",
        "sputter_anneal.py",
        "dp_dbba_evaporation.py",
        "npg_annealing.py",
    ]
    for name in workflow_files:
        text = (PROJECT_ROOT / "npg_chamber" / "workflows" / name).read_text(encoding="utf-8")
        assert "run_legacy_workflow" in text
        assert "exact packaged" in text
        assert "original_scripts_backup" not in text
