from __future__ import annotations

import ast
import csv
import types
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE2_PATH = PROJECT_ROOT / "npg_chamber" / "phase_scripts" / "02_sputtering_annealing.py"


def _load_data_logger() -> type:
    tree = ast.parse(PHASE2_PATH.read_text(encoding="utf-8"))
    logger_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DataLogger"
    )
    module = ast.Module(body=[logger_node], type_ignores=[])
    ast.fix_missing_locations(module)
    isolated = types.ModuleType("phase2_data_logger_test")
    isolated.__dict__.update(
        {
            "__name__": isolated.__name__,
            "csv": csv,
            "os": __import__("os"),
            "now_str": lambda: datetime(2026, 9, 4, 12, 0, 0).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    exec(compile(module, str(PHASE2_PATH), "exec"), isolated.__dict__)
    return isolated.DataLogger


def test_phase2_csv_header_and_snapshot_have_matching_columns(tmp_path: Path) -> None:
    logger = _load_data_logger()(str(tmp_path))
    snapshot = {
        "cycle": 1,
        "stage": "SPUTTER",
        "pressure_mbar": 2.0e-5,
        "oven_pv_c": 620.0,
        "oven_sv_c": 620.0,
        "keysight_voltage_v": 0.0,
        "keysight_current_a": 0.0,
        "coscon_mode": "Operate",
        "coscon_interlock": "Ok",
        "coscon_details": "Ready",
        "coscon_energy_v": 2250.0,
        "coscon_emission_a": 0.010,
        "coscon_filament_a": 0.012,
        "coscon_energy_current_a": 0.001,
        "coscon_anode_voltage_v": 100.0,
        "coscon_repeller_voltage_v": 20.0,
        "coscon_emission_bad_samples": 0,
        "phase_remaining_s": 120.0,
        "phase_total_s": 1200.0,
        "phase_timer_label": "Sputtering left",
    }
    logger.log_snapshot(snapshot, note="test")
    logger.close()

    with (tmp_path / "sputter_anneal_log.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    assert len(rows) == 2
    assert len(rows[0]) == len(rows[1]) == 22
    assert rows[0][12:17] == [
        "coscon_emission_a",
        "coscon_filament_a",
        "coscon_energy_current_a",
        "coscon_anode_voltage_v",
        "coscon_repeller_voltage_v",
    ]
    assert rows[1][12:17] == ["0.01", "0.012", "0.001", "100.0", "20.0"]
