from pathlib import Path

from npg_chamber.common.paths import PHASE_DATA_FOLDERS, ensure_all_data_sample_folders, phase_data_dir


def test_data_samples_phase_folders_exist_or_are_created():
    ensure_all_data_sample_folders()
    root = Path("Data Samples")
    assert root.is_dir()
    for folder_name in PHASE_DATA_FOLDERS.values():
        assert (root / folder_name).is_dir()


def test_phase_data_dir_returns_expected_names():
    assert phase_data_dir("heat").name == "Heat up + Calibration Data"
    assert phase_data_dir("sputter").name == "Sputtering-Annealing Data"
    assert phase_data_dir("dpdbba").name == "DP-DBBA Evaporation Data"
    assert phase_data_dir("anneal").name == "NPG Annealing Data"
