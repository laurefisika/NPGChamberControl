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


def test_no_duplicated_markdown_documentation_files():
    # User-facing instructions are intentionally consolidated into one SOP.
    removed_duplicate_paths = [
        Path("README.md"),
        Path("docs"),
        Path("installation_notes"),
        Path("TEST_REPORT.md"),
    ]
    for path in removed_duplicate_paths:
        assert not path.exists(), f"Duplicated documentation should not exist: {path}"


def test_useful_support_documents_are_kept():
    for path in ["CHANGELOG.md", "LICENSE.md", "SOURCE_CODE_MANIFEST.json"]:
        p = Path(path)
        assert p.is_file(), path
        assert p.read_text(encoding="utf-8").strip(), path


def test_distribution_does_not_ship_internal_change_note_markdown():
    allowed = {"CHANGELOG.md", "LICENSE.md", "READ ME.md"}
    root_markdown = {path.name for path in Path(".").glob("*.md")}
    assert root_markdown == allowed


def test_dead_helper_modules_remain_removed():
    removed_paths = [
        Path("npg_chamber/config/defaults.py"),
        Path("npg_chamber/common/prompts.py"),
        Path("npg_chamber/common/timing.py"),
    ]
    for path in removed_paths:
        assert not path.exists(), f"Unused helper should not exist: {path}"


def test_release_metadata_matches_v17_9_publication() -> None:
    citation = Path("CITATION.cff").read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    assert "version: 0.9.36" in citation
    assert "date-released: 2026-08-11" in citation
    assert "Repository publication correction · 2026-08-18" in changelog
    assert "include READ?ME.md" in manifest
    assert "include READ ME.md" not in manifest
