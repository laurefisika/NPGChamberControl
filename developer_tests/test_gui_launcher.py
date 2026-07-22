from pathlib import Path



def test_gui_launcher_source_has_readme_button():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert "READ ME" in text
    assert "open_readme" in text


def test_gui_launcher_has_requested_aesthetic_colors_and_inputs():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert 'font=("Segoe UI", 44, "bold")' in text
    assert '#f4a6a6' in text  # phase 1 pastel red
    assert '#8db7f5' in text  # phase 2 pastel blue
    assert '#f2d36b' in text  # phase 3 pastel yellow
    assert '#9bd8a5' in text  # phase 4 pastel green
    assert '#86efac' in text  # READ ME green
    assert '#ef4444' in text  # Close red
    assert 'Run name' in text
    assert 'Ratio source' in text
    assert 'Automatic after Phase 01' in text
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


def test_close_button_stops_running_phase():
    text = Path("npg_chamber/gui_launcher.py").read_text(encoding="utf-8")
    assert "command=self.request_close" in text
    assert "terminate_process(process)" in text
    assert 'self.root.protocol("WM_DELETE_WINDOW", self.request_close)' in text
    assert "Close requested: stopping the running phase process" in text


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
    assert "hard safety limits are intentionally not editable" in text


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
