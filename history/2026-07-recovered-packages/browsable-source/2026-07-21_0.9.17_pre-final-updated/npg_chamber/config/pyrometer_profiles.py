"""Persistent user-defined pyrometer material profiles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from npg_chamber.common.paths import data_samples_dir
from npg_chamber.config.run_parameters import (
    pyrometer_default_values,
    validate_pyrometer_values,
)

PROFILE_STORE_VERSION = 1
VALIDATED_PROFILE_NAME = "Au/mica — validated"


def profile_store_path() -> Path:
    path = data_samples_dir() / "Configuration" / "pyrometer_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validated_profile() -> dict[str, Any]:
    return pyrometer_default_values()


def load_pyrometer_profiles() -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {
        VALIDATED_PROFILE_NAME: _validated_profile(),
    }
    path = profile_store_path()
    if not path.exists():
        return profiles

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_profiles = payload.get("profiles", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_profiles, dict):
            return profiles
        for name, raw in raw_profiles.items():
            if name == VALIDATED_PROFILE_NAME or not isinstance(raw, dict):
                continue
            candidate = dict(raw)
            candidate["profile_name"] = str(name).strip()
            profiles[str(name).strip()] = validate_pyrometer_values(candidate)
    except Exception:
        # A damaged optional profile file must never prevent the launcher from
        # opening. The operator can recreate profiles from the validated one.
        return profiles
    return profiles


def _write_custom_profiles(profiles: Mapping[str, Mapping[str, Any]]) -> Path:
    custom: dict[str, dict[str, Any]] = {}
    for name, values in profiles.items():
        clean_name = str(name).strip()
        if not clean_name or clean_name == VALIDATED_PROFILE_NAME:
            continue
        candidate = dict(values)
        candidate["profile_name"] = clean_name
        custom[clean_name] = validate_pyrometer_values(candidate)

    path = profile_store_path()
    payload = {"version": PROFILE_STORE_VERSION, "profiles": custom}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def save_pyrometer_profile(name: str, values: Mapping[str, Any]) -> Path:
    clean_name = str(name).strip()
    if not clean_name:
        raise ValueError("Profile name cannot be empty")
    if clean_name == VALIDATED_PROFILE_NAME:
        raise ValueError("The validated Au/mica profile is read-only")

    profiles = load_pyrometer_profiles()
    candidate = dict(values)
    candidate["profile_name"] = clean_name
    profiles[clean_name] = validate_pyrometer_values(candidate)
    return _write_custom_profiles(profiles)


def delete_pyrometer_profile(name: str) -> Path:
    clean_name = str(name).strip()
    if clean_name == VALIDATED_PROFILE_NAME:
        raise ValueError("The validated Au/mica profile cannot be deleted")
    profiles = load_pyrometer_profiles()
    profiles.pop(clean_name, None)
    return _write_custom_profiles(profiles)
