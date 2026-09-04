from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from npg_chamber import installation_check

ROOT = Path(__file__).resolve().parents[1]


def test_installation_check_passes_current_tree() -> None:
    assert installation_check.verify("2026.09.04-r20") is True


def test_installation_check_cli_passes_without_importing_phase_scripts() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "npg_chamber.installation_check", "--expected-build", "2026.09.04-r20"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OK]   Build identity" in result.stdout
    assert "Source verification passed." in result.stdout


def test_batch_launcher_uses_dedicated_verifier_not_inline_source_literals() -> None:
    text = (ROOT / "START_NPG_CHAMBER.bat").read_text(encoding="utf-8")
    assert '-m npg_chamber.installation_check --expected-build "%SOURCE_BUILD%"' in text
    assert 'TEMP_SLOPE_WINDOW_S = 45.0' not in text
    assert 'apply_mode_button.clicked.connect(self._apply_feedback_mode)' not in text
    assert 'SOURCE_BUILD=2026.09.04-r20' in text
