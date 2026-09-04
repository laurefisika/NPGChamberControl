from __future__ import annotations

from pathlib import Path

import pytest

from npg_chamber.config import pyrometer_profiles


def test_custom_profile_can_be_saved_loaded_and_deleted(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "pyrometer_profiles.json"
    monkeypatch.setattr(pyrometer_profiles, "profile_store_path", lambda: store)

    values = {
        "enabled": True,
        "profile_name": "Graphite",
        "emissivity_percent": 80,
        "sample_slope": 1.2,
        "sample_intercept_c": 5.0,
        "minimum_valid_pyrometer_c": 100.0,
        "write_emissivity_at_start": True,
        "default_view": "sample",
    }

    saved_path = pyrometer_profiles.save_pyrometer_profile("Graphite", values)
    assert saved_path == store
    assert store.is_file()

    profiles = pyrometer_profiles.load_pyrometer_profiles()
    assert pyrometer_profiles.VALIDATED_PROFILE_NAME in profiles
    assert profiles["Graphite"]["emissivity_percent"] == 80
    assert profiles["Graphite"]["default_view"] == "sample"

    pyrometer_profiles.delete_pyrometer_profile("Graphite")
    profiles_after = pyrometer_profiles.load_pyrometer_profiles()
    assert "Graphite" not in profiles_after
    assert pyrometer_profiles.VALIDATED_PROFILE_NAME in profiles_after


def test_validated_profile_cannot_be_overwritten_or_deleted(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "pyrometer_profiles.json"
    monkeypatch.setattr(pyrometer_profiles, "profile_store_path", lambda: store)

    with pytest.raises(ValueError):
        pyrometer_profiles.save_pyrometer_profile(
            pyrometer_profiles.VALIDATED_PROFILE_NAME,
            pyrometer_profiles.load_pyrometer_profiles()[pyrometer_profiles.VALIDATED_PROFILE_NAME],
        )
    with pytest.raises(ValueError):
        pyrometer_profiles.delete_pyrometer_profile(pyrometer_profiles.VALIDATED_PROFILE_NAME)


def test_damaged_optional_profile_file_falls_back_to_validated_profile(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "pyrometer_profiles.json"
    store.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(pyrometer_profiles, "profile_store_path", lambda: store)

    profiles = pyrometer_profiles.load_pyrometer_profiles()
    assert list(profiles) == [pyrometer_profiles.VALIDATED_PROFILE_NAME]
