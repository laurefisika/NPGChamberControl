import re
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
    assert "## Release history at a glance" in changelog
    assert "## Historical release record (v16 and earlier)" in changelog
    for historical_version in (
        "## 0.9.22 - Project archive v16",
        "## 0.9.21 - Project archive v15",
        "## 0.9.20 - Project archive v14",
        "## 0.9.13 - Automated COSCON control in Phase 02",
        "## 0.9.0 — Centralized Data Samples output folders",
        "## 0.8.5 — Final source verification",
    ):
        assert historical_version in changelog
    assert "include READ?ME.md" in manifest
    assert "include READ ME.md" not in manifest


def test_project_credits_and_documentation_license_are_explicit() -> None:
    readme = Path("READ ME.md").read_text(encoding="utf-8")
    license_summary = Path("LICENSE.md").read_text(encoding="utf-8")
    legal_text = Path("LICENSES/CC-BY-4.0.txt").read_text(encoding="utf-8")
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    for credit in (
        "Laura Rodríguez Jordán",
        "Roger Simon de Febrer",
        "Piotr Krzysztof Ciochon",
    ):
        assert credit in readme
        assert credit in license_summary

    assert "Creative Commons Attribution 4.0 International" in license_summary
    assert "Creative Commons Attribution 4.0 International Public License" in legal_text
    assert "include LICENSES/CC-BY-4.0.txt" in manifest


def test_phase_explanations_are_concise_single_page_pdfs() -> None:
    explanation_dir = Path("npg_chamber/script_explanations")
    expected = {
        "01_heat_up_calibration_explanation.pdf",
        "02_sputtering_annealing_explanation.pdf",
        "03_dp_dbba_evaporation_explanation.pdf",
        "04_npg_annealings_explanation.pdf",
    }

    assert {path.name for path in explanation_dir.glob("*.pdf")} == expected
    for filename in expected:
        data = (explanation_dir / filename).read_bytes()
        assert data.startswith(b"%PDF-")
        assert len(re.findall(rb"/Type\s*/Page\b", data)) == 1
