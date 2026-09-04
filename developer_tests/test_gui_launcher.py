from pathlib import Path



def test_gui_launcher_source_has_readme_button():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'text="README"' in text
    assert "open_readme" in text


def test_gui_launcher_has_requested_aesthetic_colors_and_inputs():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'font=("Segoe UI", 44, "bold")' in text
    assert '#f4a6a6' in text  # phase 1 pastel red
    assert '#8db7f5' in text  # phase 2 pastel blue
    assert '#f2d36b' in text  # phase 3 pastel yellow
    assert '#9bd8a5' in text  # phase 4 pastel green
    assert '#86efac' in text  # README green
    assert '#ef4444' in text  # Close red
    assert 'Run name' in text
    assert 'Ratio source' in text
    assert 'Phase 01 ratio will be confirmed before Phase 03' in text
    assert 'Thickness ratio required' in text
    assert 'NPG_CHAMBER_RUN_NAME' in text
    assert 'NPG_CHAMBER_THICKNESS_RATIO' in text


def test_gui_launcher_creates_tk_root_before_stringvars():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    root_index = text.index("self.root = tk.Tk()")
    ratio_var_index = text.index("self.dp_ratio_status_var = tk.StringVar")
    status_var_index = text.index("self.status_var = tk.StringVar")
    assert root_index < ratio_var_index
    assert root_index < status_var_index


def test_gui_uses_light_background():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert '#f7f9fc' in text


def test_close_button_never_force_kills_running_phase():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    runner = Path("npg_chamber/workflows/runner.py").read_text(encoding="utf-8")
    assert "command=self.request_close" in text
    assert 'self.root.protocol("WM_DELETE_WINDOW", self.request_close)' in text
    assert "Launcher close blocked while a phase is active" in text
    assert "Use Abort / Safe Stop inside the phase GUI" in text
    assert "terminate_process" not in text
    assert "taskkill" not in runner
    assert "def terminate_process" not in runner


def test_gui_has_high_contrast_pastel_card_design():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'card_bg="#fff1f2"' in text
    assert 'card_bg="#eef6ff"' in text
    assert 'card_bg="#fff8db"' in text
    assert 'card_bg="#effaf1"' in text
    assert 'fg="#111827"' in text
    assert 'relief="solid"' in text
    assert 'highlightbackground="#111827"' in text


def test_gui_has_phase_explanation_buttons_and_pdf_paths():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'text=f"Explanation {phase.number} {phase.title}"' in text
    assert 'command=lambda key=phase.key: self.open_explanation(key)' in text
    for pdf_name in [
        "01_heat_up_calibration_explanation.pdf",
        "02_sputtering_annealing_explanation.pdf",
        "03_dp_dbba_evaporation_explanation.pdf",
        "04_npg_annealings_explanation.pdf",
    ]:
        assert pdf_name in text
        assert Path("npg_chamber/script_explanations").joinpath(pdf_name).is_file()


def test_gui_has_run_only_automation_parameter_editor():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'text="Change automatization parameters"' in text
    assert "open_automation_parameters" in text
    assert 'dialog.geometry("1100x800")' in text
    assert 'dialog.resizable(True, True)' in text
    assert 'dialog.state("zoomed")' not in text
    assert 'dialog.attributes("-zoomed", True)' not in text
    assert 'dialog.transient(self.root)' not in text
    assert 'dialog.bind("<MouseWheel>"' in text
    assert 'dialog.bind("<Button-4>"' in text
    assert 'dialog.bind("<Button-5>"' in text
    assert "AUTOMATION_PARAMETERS_ENV" in text
    assert "encode_overrides" in text
    assert "Apply for this launcher run" in text
    assert "BASIC_PARAMETER_KEYS" in text
    assert 'text="Basic controls"' in text
    assert 'text="Expert mode  ▸"' in text
    assert 'button.configure(text="Expert mode  ▾")' in text


def test_gui_has_shared_pyrometer_profile_editor() -> None:
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'notebook.add(pyro_tab, text="Pyrometer")' in text
    assert "Configure globally, view locally" in text
    assert "Restore validated Au/mica mode" not in text
    assert "New mode" in text
    assert "Save mode" in text
    assert "Delete" in text
    assert "PYROMETER_PARAMETERS_ENV" in text
    assert "encode_pyrometer_settings" in text
    assert "Pyrometer raw" in text
    assert "Sample estimate" in text
    assert "Confirm pyrometer emissivity" in text
    assert "if it differs, and verified by readback" in text


def test_launcher_exposes_persistent_full_chamber_automation_modes():
    source = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert "Saved automation modes" in source
    assert "One-click chamber recipe setup" in source
    assert "Save current as mode" in source
    assert "Load mode" in source
    assert "NPG_CHAMBER_AUTOMATION_MODE_NAME" in Path("npg_chamber/config/run_parameters.py").read_text(encoding="utf-8")



def test_phase03_ratio_is_always_operator_confirmed_when_available() -> None:
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert "Do you agree with the thickness ratio obtained?" in text
    assert "Thickness ratio obtained in Phase 1:" in text
    assert "Yes: continue with this ratio." in text
    assert "No: modify the ratio before starting Phase 03." in text
    assert "def _ask_manual_thickness_ratio(" in text
    assert "operator-modified value before Phase 03" in text


def test_phase03_ratio_confirmation_yes_reuses_phase1_value() -> None:
    from npg_chamber.gui_launcher import NPGLauncherApp

    class MessageBox:
        def __init__(self):
            self.prompt = None

        def askyesno(self, title, prompt, **kwargs):
            self.prompt = (title, prompt, kwargs)
            return True

    app = NPGLauncherApp.__new__(NPGLauncherApp)
    app.root = object()
    app.messagebox = MessageBox()
    app.simpledialog = object()  # Must not be used on the Yes path.
    app.session_thickness_ratio = 1.2345
    app.session_ratio_source = "Phase 01"

    assert app._get_or_ask_thickness_ratio() == 1.2345
    assert app.messagebox.prompt is not None
    assert "Thickness ratio obtained in Phase 1: 1.2345" in app.messagebox.prompt[1]


def test_phase03_ratio_confirmation_no_allows_operator_replacement() -> None:
    from npg_chamber.gui_launcher import NPGLauncherApp

    class RatioStatus:
        def __init__(self):
            self.value = None

        def set(self, value):
            self.value = value

    class MessageBox:
        def askyesno(self, *args, **kwargs):
            return False

        def showerror(self, *args, **kwargs):
            raise AssertionError("Valid replacement should not show an error")

    class SimpleDialog:
        def __init__(self):
            self.kwargs = None

        def askstring(self, *args, **kwargs):
            self.kwargs = kwargs
            return "2,75"

    app = NPGLauncherApp.__new__(NPGLauncherApp)
    app.root = object()
    app.messagebox = MessageBox()
    app.simpledialog = SimpleDialog()
    app.dp_ratio_status_var = RatioStatus()
    app.session_thickness_ratio = 1.2345
    app.session_ratio_source = "Phase 01"

    assert app._get_or_ask_thickness_ratio() == 2.75
    assert app.session_thickness_ratio == 2.75
    assert app.session_ratio_source == "operator-modified value before Phase 03"
    assert app.simpledialog.kwargs["initialvalue"] == "1.2345"
    assert "2.75" in app.dp_ratio_status_var.value


def test_request_close_is_behaviorally_blocked_while_phase_process_runs() -> None:
    import threading
    from types import SimpleNamespace

    from npg_chamber.gui_launcher import NPGLauncherApp

    class RunningProcess:
        def poll(self):
            return None

    class FakeRoot:
        def __init__(self):
            self.quit_called = False
            self.destroy_called = False

        def quit(self):
            self.quit_called = True

        def destroy(self):
            self.destroy_called = True

    warnings = []
    statuses = []
    app = NPGLauncherApp.__new__(NPGLauncherApp)
    app.process_lock = threading.Lock()
    app.current_process = RunningProcess()
    app.running_key = "heat"
    app.closing_requested = False
    app.root = FakeRoot()
    app.status_var = SimpleNamespace(set=statuses.append)
    app.messagebox = SimpleNamespace(
        showwarning=lambda title, message, parent=None: warnings.append((title, message, parent))
    )

    app.request_close()

    assert app.closing_requested is False
    assert app.root.quit_called is False
    assert app.root.destroy_called is False
    assert warnings and warnings[0][0] == "Phase still running"
    assert "Abort / Safe Stop" in warnings[0][1]
    assert statuses and "Close blocked" in statuses[-1]


def test_request_close_closes_launcher_after_phase_has_finished() -> None:
    import threading
    from types import SimpleNamespace

    from npg_chamber.gui_launcher import NPGLauncherApp

    class FinishedProcess:
        def poll(self):
            return 0

    class FakeRoot:
        def __init__(self):
            self.quit_called = False
            self.destroy_called = False

        def quit(self):
            self.quit_called = True

        def destroy(self):
            self.destroy_called = True

    app = NPGLauncherApp.__new__(NPGLauncherApp)
    app.process_lock = threading.Lock()
    app.current_process = FinishedProcess()
    app.running_key = None
    app.closing_requested = False
    app.root = FakeRoot()
    app.status_var = SimpleNamespace(set=lambda _value: None)
    app.messagebox = SimpleNamespace(showwarning=lambda *args, **kwargs: None)

    app.request_close()

    assert app.closing_requested is True
    assert app.root.quit_called is True
    assert app.root.destroy_called is True
