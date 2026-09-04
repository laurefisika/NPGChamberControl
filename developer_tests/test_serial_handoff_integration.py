from pathlib import Path


def test_launcher_blocks_next_phase_until_verified_serial_handoff():
    runner = Path("npg_chamber/workflows/runner.py").read_text(encoding="utf-8")
    gui = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")

    assert 'verify_all_chamber_ports_released(context=f"before phase {key}")' in runner
    assert "verify_all_chamber_ports_released(context=label)" in runner
    assert "SerialHandoffError" in gui
    assert "The next phase was blocked" in gui
    assert "All chamber COM ports were reset and released" in gui


def test_phase_two_and_four_reset_buffers_before_closing_serial_ports():
    phase2 = Path("npg_chamber/phase_scripts/02_sputtering_annealing.py").read_text(
        encoding="utf-8"
    )
    phase4 = Path("npg_chamber/phase_scripts/04_npg_annealings.py").read_text(
        encoding="utf-8"
    )

    assert "reset_serial_buffers_and_close" in phase2
    assert '"XGS600 pressure port"' in phase2
    assert '"Oven PID port"' in phase2
    assert '"Keysight power-supply port"' in phase2

    assert "reset_serial_buffers_and_close" in phase4
    assert '"Oven PID port"' in phase4
    assert '"Keysight power-supply port"' in phase4
    assert '"Arduino CK-1 temperature port"' in phase4
