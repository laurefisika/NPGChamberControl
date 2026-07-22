from __future__ import annotations

from pathlib import Path

import pytest

from npg_chamber.config import automation_modes
from npg_chamber.config.run_parameters import all_default_values, pyrometer_default_values


def _mode(description: str = "NPG recipe") -> dict:
    phases = all_default_values()
    phases["sputter"]["anneal_target_c"] = 600.0
    pyrometer = pyrometer_default_values()
    return {"description": description, "phases": phases, "pyrometer": pyrometer}


def test_full_automation_mode_can_be_saved_loaded_and_deleted(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "automation_modes.json"
    monkeypatch.setattr(automation_modes, "mode_store_path", lambda: store)

    saved = automation_modes.save_automation_mode("NPG at 600 C", _mode())
    assert saved == store
    modes = automation_modes.load_automation_modes()
    assert automation_modes.PACKAGED_DEFAULT_MODE_NAME in modes
    assert modes["NPG at 600 C"]["phases"]["sputter"]["anneal_target_c"] == 600.0
    assert modes["NPG at 600 C"]["description"] == "NPG recipe"

    automation_modes.delete_automation_mode("NPG at 600 C")
    assert "NPG at 600 C" not in automation_modes.load_automation_modes()


def test_packaged_default_mode_is_protected(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "automation_modes.json"
    monkeypatch.setattr(automation_modes, "mode_store_path", lambda: store)
    with pytest.raises(ValueError):
        automation_modes.save_automation_mode(
            automation_modes.PACKAGED_DEFAULT_MODE_NAME,
            _mode(),
        )
    with pytest.raises(ValueError):
        automation_modes.delete_automation_mode(automation_modes.PACKAGED_DEFAULT_MODE_NAME)


def test_damaged_optional_mode_file_falls_back_to_packaged_defaults(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "automation_modes.json"
    store.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(automation_modes, "mode_store_path", lambda: store)
    modes = automation_modes.load_automation_modes()
    assert list(modes) == [automation_modes.PACKAGED_DEFAULT_MODE_NAME]
