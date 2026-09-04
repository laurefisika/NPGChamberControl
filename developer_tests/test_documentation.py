from pathlib import Path


def test_readme_is_the_single_useful_user_guide():
    readme = Path("README.md")
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
        "README",
        "Troubleshooting",
        "<phase name> <sample name> data <two-digit counter>",
        "Heat up + Calibration SC67 data 00",
        "Sputtering-Annealing SC67 data 00",
        "DP-DBBA Evaporation SC67 data 00",
        "NPG Annealings SC67 data 00",
        "2026.09.04-r20",
    ]
    for term in required_terms:
        assert term in text

    assert "READ ME.md" not in text
    assert "--run-legacy" not in text


def test_documentation_filenames_are_standardized():
    assert Path("README.md").is_file()
    assert not Path("READ ME.md").exists()
    assert not Path("TEST_REPORT.md").exists()
    assert not Path("installation_notes").exists()


def test_useful_support_documents_are_kept():
    for path in ["CHANGELOG.md", "LICENSE.md", "SOURCE_CODE_MANIFEST.json"]:
        document = Path(path)
        assert document.is_file(), path
        assert document.read_text(encoding="utf-8").strip(), path


def test_distribution_has_no_untracked_root_markdown_files():
    allowed = {"CHANGELOG.md", "LICENSE.md", "README.md"}
    root_markdown = {path.name for path in Path(".").glob("*.md")}
    assert root_markdown == allowed


def test_retired_helper_modules_remain_removed():
    removed_paths = [
        Path("npg_chamber/config/defaults.py"),
        Path("npg_chamber/common/prompts.py"),
        Path("npg_chamber/common/timing.py"),
        Path("npg_chamber/legacy_scripts"),
        Path("npg_chamber/workflows/legacy_runner.py"),
    ]
    for path in removed_paths:
        assert not path.exists(), f"Retired project item should not exist: {path}"
