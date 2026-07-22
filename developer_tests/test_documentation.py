from pathlib import Path


def test_unified_readme_exists_and_is_useful():
    readme = Path("READ ME.md")
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    required_terms = [
        "Standard Operating Procedure",
        "npg-chamber",
        "Heat up + Calibration",
        "Sputtering-Annealing",
        "DP-DBBA Evaporation",
        "NPG Annealings",
        "--run heat",
        "--run sputter",
        "--run dpdbba",
        "--run anneal",
        "GUI buttons",
        "Fallback",
        "Troubleshooting",
    ]
    for term in required_terms:
        assert term in text


def test_repository_documentation_is_structured_without_obsolete_duplicates():
    # READ ME.md remains the operating SOP; README.md and docs/ provide the
    # professional repository overview without duplicating the old package notes.
    assert Path("README.md").is_file()
    assert Path("docs").is_dir()
    for removed_path in [Path("installation_notes"), Path("TEST_REPORT.md")]:
        assert not removed_path.exists(), f"Obsolete documentation remains: {removed_path}"


def test_useful_support_documents_are_kept():
    for path in [
        "CHANGELOG.md",
        "CITATION.cff",
        "LICENSE.md",
        "SECURITY.md",
        "SOURCE_CODE_MANIFEST.json",
    ]:
        p = Path(path)
        assert p.is_file(), path
        assert p.read_text(encoding="utf-8").strip(), path


def test_original_script_backup_is_kept_and_documented():
    backup = Path("original_scripts_backup")
    assert backup.is_dir()
    assert len(list(backup.glob("*.py"))) == 4

    readme_text = Path("READ ME.md").read_text(encoding="utf-8")
    changelog_text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    assert "original_scripts_backup/" in readme_text
    assert "recovery/reference" in readme_text
    assert "Original-script backup preservation" in changelog_text


def test_dead_helper_modules_remain_removed():
    removed_paths = [
        Path("npg_chamber/config/defaults.py"),
        Path("npg_chamber/common/prompts.py"),
        Path("npg_chamber/common/timing.py"),
    ]
    for path in removed_paths:
        assert not path.exists(), f"Unused helper should not exist: {path}"
