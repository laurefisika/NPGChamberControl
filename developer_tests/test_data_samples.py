from pathlib import Path

from npg_chamber.common.paths import (
    create_numbered_run_dir,
    phase_data_dir,
    phase_run_folder_prefix,
    safe_run_name,
)


def test_phase_data_dir_returns_expected_names():
    assert phase_data_dir("heat").name == "Heat up + Calibration Data"
    assert phase_data_dir("sputter").name == "Sputtering-Annealing Data"
    assert phase_data_dir("dpdbba").name == "DP-DBBA Evaporation Data"
    assert phase_data_dir("anneal").name == "NPG Annealing Data"


def test_every_phase_uses_the_same_numbered_run_folder_pattern(tmp_path):
    expected = {
        "heat": "Heat up + Calibration SC67 data 00",
        "sputter": "Sputtering-Annealing SC67 data 00",
        "dpdbba": "DP-DBBA Evaporation SC67 data 00",
        "anneal": "NPG Annealings SC67 data 00",
    }
    for key, folder_name in expected.items():
        phase_parent = tmp_path / key
        first = create_numbered_run_dir(key, "SC67", parent=phase_parent)
        second = create_numbered_run_dir(key, "SC67", parent=phase_parent)
        assert first.name == folder_name
        assert second.name == folder_name[:-2] + "01"


def test_run_folder_name_is_windows_safe_and_prefix_is_shared():
    assert safe_run_name('  SC:67 / repeat  ') == "SC_67 _ repeat"
    assert phase_run_folder_prefix("heat", '  SC:67 / repeat  ') == (
        "Heat up + Calibration SC_67 _ repeat data"
    )


def test_all_runtime_phases_use_the_shared_numbered_allocator():
    root = Path("npg_chamber/phase_scripts")
    expected_calls = {
        "01_heat_up_calibration.py": 'create_numbered_run_dir("heat"',
        "02_sputtering_annealing.py": 'create_numbered_run_dir("sputter"',
        "03_dp_dbba_evaporation.py": 'create_numbered_run_dir("dpdbba"',
        "04_npg_annealings.py": 'create_numbered_run_dir("anneal"',
    }
    for filename, call in expected_calls.items():
        source = (root / filename).read_text(encoding="utf-8")
        assert call in source
