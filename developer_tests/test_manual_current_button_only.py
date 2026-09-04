from pathlib import Path


PHASE_FILES = (
    "01_heat_up_calibration.py",
    "03_dp_dbba_evaporation.py",
)


def test_manual_current_textbox_has_no_submit_binding():
    root = Path(__file__).resolve().parents[1]
    for filename in PHASE_FILES:
        source = (root / "npg_chamber" / "phase_scripts" / filename).read_text(encoding="utf-8")
        manual_branch = source.split("elif target == 'manual':", 1)[1].split("else:", 1)[0]
        assert "textbox.on_submit(" not in manual_branch
        assert "live_manual_textboxes[key] = textbox" in manual_branch
        assert "btn_manual_set.on_clicked(_apply_manual_current_from_widgets)" in source
