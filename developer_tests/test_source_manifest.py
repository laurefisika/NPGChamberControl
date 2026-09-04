from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_runtime_files_match_clean_manifest() -> None:
    manifest = json.loads((PROJECT_ROOT / "SOURCE_CODE_MANIFEST.json").read_text(encoding="utf-8"))
    expected = {"heat", "sputter", "dpdbba", "anneal"}
    assert {entry["workflow_key"] for entry in manifest} == expected
    for entry in manifest:
        resource_path = PROJECT_ROOT / entry["packaged_resource"]
        assert resource_path.is_file()
        assert sha256(resource_path) == entry["sha256"]
        assert resource_path.stat().st_size == entry["size_bytes"]
        assert "backup_resource" not in entry
        assert "packaging_note" not in entry


def test_distribution_has_no_runtime_backup_directories() -> None:
    assert not (PROJECT_ROOT / "original_scripts_backup").exists()
    assert not (PROJECT_ROOT / "migration_backups").exists()


def test_workflow_entry_points_delegate_only_to_packaged_runtime_scripts() -> None:
    workflow_files = [
        "heat_calibration.py", "sputter_anneal.py",
        "dp_dbba_evaporation.py", "npg_annealing.py",
    ]
    for name in workflow_files:
        text = (PROJECT_ROOT / "npg_chamber" / "workflows" / name).read_text(encoding="utf-8")
        assert "run_workflow" in text
