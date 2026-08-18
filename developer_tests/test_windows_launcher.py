from pathlib import Path


def test_windows_batch_launcher_exists_and_is_relative():
    bat = Path("START_NPG_CHAMBER.bat")
    assert bat.is_file()
    text = bat.read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in text
    assert '.venv\\Scripts\\python.exe' in text
    assert 'python -m npg_chamber' in text or '-m npg_chamber' in text
    assert 'C:\\Users' not in text


def test_readme_mentions_windows_batch_launcher():
    text = Path("READ ME.md").read_text(encoding="utf-8")
    assert "START_NPG_CHAMBER.bat" in text
    assert "One-click start on Windows" in text


def test_batch_launcher_does_not_pause_on_normal_gui_close():
    text = Path("START_NPG_CHAMBER.bat").read_text(encoding="utf-8")
    normal_exit_block = 'if not "%EXITCODE%"=="0"'
    assert normal_exit_block in text
    assert 'The launcher closed normally.' not in text
    assert 'pause\nexit /b %EXITCODE%' not in text.replace('\r\n', '\n')


def test_windows_launcher_reuses_and_repairs_local_runtime() -> None:
    text = Path("START_NPG_CHAMBER.bat").read_text(encoding="utf-8")
    assert "import webview.platforms.winforms" not in text
    assert "u.find_spec(m)" in text
    assert "'webview'" in text
    assert "'clr'" in text
    assert "--no-cache-dir" in text
    assert "--no-deps -e ." in text
    assert "python -m venv .venv" in text
    assert "py -3" not in text
    assert "Installing NPG Chamber dependencies once" in text
    assert "Repairing the local project link" in text
    assert "Less than 700 MB" in text
