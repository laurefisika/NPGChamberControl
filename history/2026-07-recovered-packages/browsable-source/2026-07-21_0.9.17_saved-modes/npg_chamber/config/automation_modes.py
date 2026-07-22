"""Persistent full-chamber automation modes for the unified launcher.

A mode stores the editable startup recipe for all four phases plus the shared
pyrometer profile. Run names, sample-specific thickness ratios, COM ports and
hard safety limits are intentionally not part of a saved mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from npg_chamber.common.paths import data_samples_dir
from npg_chamber.config.run_parameters import (
    PHASE_PARAMETER_SPECS,
    all_default_values,
    pyrometer_default_values,
    validate_phase_values,
    validate_pyrometer_values,
)

MODE_STORE_VERSION = 1
PACKAGED_DEFAULT_MODE_NAME = "Packaged defaults"


def mode_store_path() -> Path:
    path = data_samples_dir() / "Configuration" / "automation_modes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _packaged_default_mode() -> dict[str, Any]:
    return {
        "description": (
            "Factory project recipe. All editable values match the packaged "
            "0.9.17 defaults."
        ),
        "phases": all_default_values(),
        "pyrometer": pyrometer_default_values(),
    }


def validate_automation_mode(values: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(values, Mapping):
        raise ValueError("Automation mode must be a mapping")

    description = str(values.get("description", "")).strip()
    raw_phases = values.get("phases")
    raw_pyrometer = values.get("pyrometer")
    if not isinstance(raw_phases, Mapping):
        raise ValueError("Automation mode must contain a phases object")
    if not isinstance(raw_pyrometer, Mapping):
        raise ValueError("Automation mode must contain a pyrometer object")

    expected_phases = set(PHASE_PARAMETER_SPECS)
    supplied_phases = set(raw_phases)
    missing = sorted(expected_phases - supplied_phases)
    unknown = sorted(supplied_phases - expected_phases)
    if missing:
        raise ValueError(f"Automation mode is missing phase(s): {', '.join(missing)}")
    if unknown:
        raise ValueError(f"Automation mode has unknown phase(s): {', '.join(unknown)}")

    phases = {
        phase: validate_phase_values(phase, raw_phases[phase])
        for phase in PHASE_PARAMETER_SPECS
    }
    pyrometer = validate_pyrometer_values(raw_pyrometer)
    return {
        "description": description,
        "phases": phases,
        "pyrometer": pyrometer,
    }


def load_automation_modes() -> dict[str, dict[str, Any]]:
    modes: dict[str, dict[str, Any]] = {
        PACKAGED_DEFAULT_MODE_NAME: _packaged_default_mode(),
    }
    path = mode_store_path()
    if not path.exists():
        return modes

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_modes = payload.get("modes", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_modes, dict):
            return modes
        for name, raw in raw_modes.items():
            clean_name = str(name).strip()
            if (
                not clean_name
                or clean_name == PACKAGED_DEFAULT_MODE_NAME
                or not isinstance(raw, dict)
            ):
                continue
            modes[clean_name] = validate_automation_mode(raw)
    except Exception:
        # Optional user presets must never prevent the launcher from opening.
        return modes
    return modes


def _write_custom_modes(modes: Mapping[str, Mapping[str, Any]]) -> Path:
    custom: dict[str, dict[str, Any]] = {}
    for name, values in modes.items():
        clean_name = str(name).strip()
        if not clean_name or clean_name == PACKAGED_DEFAULT_MODE_NAME:
            continue
        custom[clean_name] = validate_automation_mode(values)

    path = mode_store_path()
    payload = {"version": MODE_STORE_VERSION, "modes": custom}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def save_automation_mode(name: str, values: Mapping[str, Any]) -> Path:
    clean_name = str(name).strip()
    if not clean_name:
        raise ValueError("Automation mode name cannot be empty")
    if clean_name == PACKAGED_DEFAULT_MODE_NAME:
        raise ValueError("The packaged-default mode is read-only")

    modes = load_automation_modes()
    modes[clean_name] = validate_automation_mode(values)
    return _write_custom_modes(modes)


def delete_automation_mode(name: str) -> Path:
    clean_name = str(name).strip()
    if clean_name == PACKAGED_DEFAULT_MODE_NAME:
        raise ValueError("The packaged-default mode cannot be deleted")
    modes = load_automation_modes()
    modes.pop(clean_name, None)
    return _write_custom_modes(modes)
