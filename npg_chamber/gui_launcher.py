"""Tkinter GUI launcher for the NPG chamber workflows.

The GUI is an external launcher and never rewrites the four packaged scripts.
It can provide startup values such as the run name, the DP-DBBA calibration
ratio, and validated run-only automation recipe overrides through environment
variables. The child phase still owns all hardware communication and control
logic.
"""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from npg_chamber import __version__, __build__
from npg_chamber.common.paths import phase_data_dir
from npg_chamber.config.automation_modes import (
    PACKAGED_DEFAULT_MODE_NAME,
    delete_automation_mode,
    load_automation_modes,
    mode_store_path,
    save_automation_mode,
    validate_automation_mode,
)
from npg_chamber.config.pyrometer_profiles import (
    VALIDATED_PROFILE_NAME,
    delete_pyrometer_profile,
    load_pyrometer_profiles,
    profile_store_path,
    save_pyrometer_profile,
)
from npg_chamber.common.serial_handoff import SerialHandoffError
from npg_chamber.config.run_parameters import (
    AUTOMATION_MODE_NAME_ENV,
    AUTOMATION_PARAMETERS_ENV,
    PYROMETER_PARAMETERS_ENV,
    all_default_values,
    encode_overrides,
    encode_pyrometer_settings,
    non_default_overrides,
    non_default_pyrometer_overrides,
    pyrometer_default_values,
    pyrometer_specs,
    specs_for_phase,
    validate_phase_values,
    validate_pyrometer_values,
)
from npg_chamber.workflows.legacy_runner import (
    LEGACY_WORKFLOWS,
    launch_legacy_workflow_process,
    wait_for_phase_process,
)


@dataclass(frozen=True)
class PhaseInfo:
    key: str
    number: str
    title: str
    description: str
    accent: str
    card_bg: str
    explanation_file: str


PHASES: list[PhaseInfo] = [
    PhaseInfo(
        key="heat",
        number="01",
        title="Heat up + Calibration",
        description="Heats the CK-1, performs QMB calibration, and obtains the thickness ratio.",
        accent="#f4a6a6",  # medium pastel red
        card_bg="#fff1f2",  # very light red card
        explanation_file="01_heat_up_calibration_explanation.pdf",
    ),
    PhaseInfo(
        key="sputter",
        number="02",
        title="Sputtering-Annealing",
        description="Guides sputtering, COSCON, Ar pressure, and the associated annealing step.",
        accent="#8db7f5",  # medium pastel blue
        card_bg="#eef6ff",  # very light blue card
        explanation_file="02_sputtering_annealing_explanation.pdf",
    ),
    PhaseInfo(
        key="dpdbba",
        number="03",
        title="DP-DBBA Evaporation",
        description="Runs DP-DBBA evaporation using the calibration ratio and CK-1 target.",
        accent="#f2d36b",  # medium pastel yellow
        card_bg="#fff8db",  # very light yellow card
        explanation_file="03_dp_dbba_evaporation_explanation.pdf",
    ),
    PhaseInfo(
        key="anneal",
        number="04",
        title="NPG Annealings",
        description="Runs the final annealing sequence and controlled Keysight ramp-down.",
        accent="#9bd8a5",  # medium pastel green
        card_bg="#effaf1",  # very light green card
        explanation_file="04_npg_annealings_explanation.pdf",
    ),
]

PHASE_BY_KEY = {phase.key: phase for phase in PHASES}
NEXT_PHASE = {
    "heat": "sputter",
    "sputter": "dpdbba",
    "dpdbba": "anneal",
}

# Keep routine operator choices immediately visible. Detailed controller gains,
# filters, quality thresholds and low-level timing remain available in the
# collapsed Expert mode section of each phase.
BASIC_PARAMETER_KEYS: dict[str, set[str]] = {
    "heat": {
        "HEATING_TRIGGER_TEMP_C", "CK1_RATE_TARGET_A_PER_S", "CALIBRATION_TARGET_SAMPLE_A",
        "EVAPORATION_CONTROL_MODE", "MOLECULE_CONDITION_PROFILE", "RATE_CONTROL_MAX_TEMP_C",
        "PID_TEMP_BAND_C", "STEPS_RAMP_UNTIL_TEMP_C",
        "DEFAULT_RAMP_UP_MODE", "KEYSIGHT_BASE_WORK_CURRENT_A", "KEYSIGHT_STEP_A",
        "STEPS_RAMP_STEP_PERIOD_S", "RAMPDOWN_STEP_A", "RAMPDOWN_STEP_PERIOD_S",
        "TEMP_WATCHDOG_MAX_TEMP_C", "KEYSIGHT_SOFT_WARNING_A",
    },
    "sputter": {
        "cycles", "start_without_degassing", "sputter_minutes", "coscon_energy_v",
        "coscon_emission_a", "anneal_target_c", "anneal_hold_minutes",
        "target_ar_pressure_mbar", "pressure_warning_mbar", "pressure_emergency_mbar",
    },
    "dpdbba": {
        "DP_DBBA_SAMPLE_EQUIVALENT_THICKNESS_A", "OVEN_TARGET_TEMPERATURE_C",
        "HEATING_TRIGGER_TEMP_C", "CK1_RATE_TARGET_A_PER_S", "EVAPORATION_CONTROL_MODE",
        "MOLECULE_CONDITION_PROFILE", "RATE_CONTROL_MAX_TEMP_C", "PID_TEMP_BAND_C",
        "STEPS_RAMP_UNTIL_TEMP_C", "DEFAULT_RAMP_UP_MODE", "KEYSIGHT_BASE_WORK_CURRENT_A",
        "KEYSIGHT_STEP_A", "STEPS_RAMP_STEP_PERIOD_S",
        "TEMP_WATCHDOG_MAX_TEMP_C", "KEYSIGHT_SOFT_WARNING_A",
    },
    "anneal": {
        "INITIAL_WAIT_S", "INITIAL_WAIT_TARGET_C", "FIRST_STAGE_TARGET_C", "FIRST_STAGE_HOLD_S",
        "SECOND_STAGE_TARGET_C", "SECOND_STAGE_HOLD_S",
        "KEYSIGHT_RAMPDOWN_STEP_A", "KEYSIGHT_RAMPDOWN_STEP_S", "FIRST_RAMPDOWN_STEP_DELAY_S",
    },
}


class NPGLauncherApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import messagebox, simpledialog, ttk

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.simpledialog = simpledialog

        # Create the Tk root before any Tk variables.
        # On Windows/Tkinter, creating StringVar before Tk() can raise:
        # "Too early to create variable: no default root window".
        self.root = tk.Tk()
        self.root.title(f"NPG Chamber Controller · v{__version__} · {__build__}")

        self.result_queue: queue.Queue[tuple[str, int, str | None]] = queue.Queue()
        self.running_key: str | None = None
        self.current_process: subprocess.Popen | None = None
        self.process_lock = threading.Lock()
        self.closing_requested = False
        self.last_completed_key: str | None = None
        self.run_name_vars: dict[str, tk.StringVar] = {}
        self._copying_phase1_run_name = False
        self.session_thickness_ratio: float | None = None
        self.session_ratio_source: str | None = None
        self.dp_ratio_status_var = tk.StringVar(value="Phase 01 ratio will be confirmed before Phase 03")
        self.automation_parameter_values = all_default_values()
        self.automation_modes = load_automation_modes()
        self.active_automation_mode_name = PACKAGED_DEFAULT_MODE_NAME
        self.pyrometer_profiles = load_pyrometer_profiles()
        self.pyrometer_parameter_values = dict(self.pyrometer_profiles[VALIDATED_PROFILE_NAME])
        self.parameter_button: tk.Button | None = None
        self.root.geometry("1120x900")
        self.root.minsize(980, 800)
        self.root.configure(bg="#f7f9fc")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)

        self._configure_style()
        self._build_layout()
        self._install_signal_handlers()
        self.root.after(250, self._poll_worker_results)

    def _install_signal_handlers(self) -> None:
        """Let Ctrl+C in CMD close the launcher and any running phase cleanly."""

        def _handler(_signum, _frame):
            print("Close requested from CMD signal.")
            try:
                self.root.after(0, self.request_close)
            except Exception:
                # Never force-kill an active hardware-control phase from the
                # launcher signal handler. The phase owns its safe-stop path.
                if self.current_process is None:
                    self.closing_requested = True

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except Exception:
                pass

    def _copy_phase1_run_name_to_other_phases(self) -> None:
        """Copy Phase 01 run-name edits to the other phase fields.

        Only Phase 01 drives this automatic copy. The other phase fields remain
        normal editable entries, so the operator can change them manually at any
        moment before launching each phase.
        """

        if self._copying_phase1_run_name:
            return
        source_var = self.run_name_vars.get("heat")
        if source_var is None:
            return

        value = source_var.get()
        self._copying_phase1_run_name = True
        try:
            for key, var in self.run_name_vars.items():
                if key != "heat":
                    var.set(value)
        finally:
            self._copying_phase1_run_name = False

    def _configure_style(self) -> None:
        self.style = self.ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.style.configure("TFrame", background="#f7f9fc")
        self.style.configure("Title.TLabel", background="#f7f9fc", foreground="#0b1220", font=("Segoe UI", 44, "bold"))
        self.style.configure("Subtitle.TLabel", background="#f7f9fc", foreground="#111827", font=("Segoe UI", 12, "bold"))
        self.style.configure("Status.TLabel", background="#f7f9fc", foreground="#111827", font=("Segoe UI", 11, "bold"))
        self.style.configure("Footer.TLabel", background="#f7f9fc", foreground="#111827", font=("Segoe UI", 9))
        self.style.configure("InputLabel.TLabel", background="#1e293b", foreground="#e2e8f0", font=("Segoe UI", 9, "bold"))

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")

        ttk.Label(header, text="NPG Chamber Controller", style="Title.TLabel", anchor="center").pack(fill="x")
        ttk.Label(
            header,
            text="Select a phase, enter the run name in the GUI, and launch the packaged final script.",
            style="Subtitle.TLabel",
            anchor="center",
        ).pack(fill="x", pady=(8, 0))

        self.status_var = tk.StringVar(value="Ready. Enter a run name and choose a phase to start.")
        status_label = ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel", anchor="center")
        status_label.pack(fill="x", pady=(22, 8))

        grid = ttk.Frame(outer)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)

        self.phase_buttons: dict[str, tk.Button] = {}
        self.explanation_buttons: dict[str, tk.Button] = {}
        for idx, phase in enumerate(PHASES):
            row = idx // 2
            col = idx % 2
            card = tk.Frame(grid, bg=phase.card_bg, highlightthickness=2, highlightbackground=phase.accent)
            card.grid(row=row, column=col, sticky="nsew", padx=9, pady=9)
            card.columnconfigure(0, weight=1)

            top_bar = tk.Frame(card, bg=phase.accent, height=6)
            top_bar.grid(row=0, column=0, sticky="ew")
            top_bar.grid_propagate(False)

            title_row = tk.Frame(card, bg=phase.card_bg)
            title_row.grid(row=1, column=0, sticky="ew", padx=18, pady=(16, 0))
            title_row.columnconfigure(1, weight=1)

            number = tk.Label(
                title_row,
                text=phase.number,
                bg=phase.card_bg,
                fg=phase.accent,
                font=("Segoe UI", 21, "bold"),
            )
            number.grid(row=0, column=0, sticky="w")

            title = tk.Label(
                title_row,
                text=f"  {phase.title}",
                bg=phase.card_bg,
                fg="#111827",
                font=("Segoe UI", 15, "bold"),
                anchor="w",
            )
            title.grid(row=0, column=1, sticky="w")

            desc = tk.Label(
                card,
                text=phase.description,
                bg=phase.card_bg,
                fg="#111827",
                font=("Segoe UI", 9),
                justify="left",
                wraplength=390,
                anchor="w",
            )
            desc.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 12))

            input_frame = tk.Frame(card, bg=phase.card_bg)
            input_frame.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 10))
            input_frame.columnconfigure(1, weight=1)

            tk.Label(
                input_frame,
                text="Run name",
                bg=phase.card_bg,
                fg="#111827",
                font=("Segoe UI", 9, "bold"),
            ).grid(row=0, column=0, sticky="w", padx=(0, 8))

            # Phase 01 acts as the default name source: edits there are copied
            # to the other fields, but each phase remains manually editable.
            run_var = tk.StringVar(value="")
            self.run_name_vars[phase.key] = run_var
            if phase.key == "heat":
                run_var.trace_add("write", lambda *_args: self._copy_phase1_run_name_to_other_phases())
            run_entry = tk.Entry(
                input_frame,
                textvariable=run_var,
                bg="#ffffff",
                fg="#111827",
                insertbackground="#111827",
                relief="flat",
                font=("Segoe UI", 10),
                highlightthickness=1,
                highlightbackground="#94a3b8",
                highlightcolor=phase.accent,
            )
            run_entry.grid(row=0, column=1, sticky="ew")

            if phase.key == "dpdbba":
                tk.Label(
                    input_frame,
                    text="Ratio source",
                    bg=phase.card_bg,
                    fg="#111827",
                    font=("Segoe UI", 9, "bold"),
                ).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
                ratio_status = tk.Label(
                    input_frame,
                    textvariable=self.dp_ratio_status_var,
                    bg=phase.card_bg,
                    fg="#111827",
                    font=("Segoe UI", 9),
                    anchor="w",
                    justify="left",
                    wraplength=260,
                )
                ratio_status.grid(row=1, column=1, sticky="ew", pady=(8, 0))

            # The visible black frame gives Start and Explanation the same clear border
            # on Windows, independently of Tk's platform-specific button rendering.
            start_border = tk.Frame(card, bg="#111827")
            start_border.grid(row=4, column=0, sticky="ew", padx=18, pady=(14, 7))
            start_border.columnconfigure(0, weight=1)

            button = tk.Button(
                start_border,
                text=f"Start {phase.number} {phase.title}",
                bg=phase.accent,
                fg="#111827",
                activebackground=phase.accent,
                activeforeground="#111827",
                relief="flat",
                bd=0,
                padx=12,
                pady=11,
                font=("Segoe UI", 10, "bold"),
                cursor="hand2",
                command=lambda key=phase.key: self.start_phase(key),
            )
            button.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
            self.phase_buttons[phase.key] = button

            explanation_border = tk.Frame(card, bg="#111827")
            explanation_border.grid(row=5, column=0, sticky="ew", padx=18, pady=(5, 18))
            explanation_border.columnconfigure(0, weight=1)

            explanation_button = tk.Button(
                explanation_border,
                text=f"Explanation {phase.number} {phase.title}",
                bg=phase.accent,
                fg="#111827",
                activebackground=phase.accent,
                activeforeground="#111827",
                relief="flat",
                bd=0,
                padx=12,
                pady=9,
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
                command=lambda key=phase.key: self.open_explanation(key),
            )
            explanation_button.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
            self.explanation_buttons[phase.key] = explanation_button

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(14, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Tip: while a phase is running, use its Abort / Safe Stop. The launcher will not force-kill hardware control.",
            style="Footer.TLabel",
        ).grid(row=0, column=0, sticky="w")

        self.parameter_button = tk.Button(
            footer,
            text="Change automatization parameters",
            bg="#c4b5fd",
            fg="#111827",
            activebackground="#a78bfa",
            activeforeground="#111827",
            relief="solid",
            bd=2,
            highlightthickness=1,
            highlightbackground="#111827",
            padx=18,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            command=self.open_automation_parameters,
        )
        self.parameter_button.grid(row=0, column=1, sticky="e", padx=(0, 8))

        readme_btn = tk.Button(
            footer,
            text="READ ME",
            bg="#86efac",
            fg="#111827",
            activebackground="#4ade80",
            activeforeground="#111827",
            relief="solid",
            bd=2,
            highlightthickness=1,
            highlightbackground="#111827",
            padx=18,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            command=self.open_readme,
        )
        readme_btn.grid(row=0, column=2, sticky="e", padx=(0, 8))

        close_btn = tk.Button(
            footer,
            text="Close",
            bg="#f87171",
            fg="#111827",
            activebackground="#ef4444",
            activeforeground="#111827",
            relief="solid",
            bd=2,
            highlightthickness=1,
            highlightbackground="#111827",
            padx=18,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            command=self.request_close,
        )
        close_btn.grid(row=0, column=3, sticky="e")


    def open_automation_parameters(self) -> None:
        """Open the run-only automation recipe editor."""

        if self.running_key is not None:
            self.messagebox.showinfo(
                "Phase running",
                "Automation parameters cannot be changed while a phase is running. "
                "Stop or finish the current phase first.",
            )
            return

        tk = self.tk
        ttk = self.ttk
        dialog = tk.Toplevel(self.root)
        dialog.title("Change automatization parameters · run only")
        dialog.geometry("1100x800")
        dialog.minsize(940, 660)
        dialog.resizable(True, True)
        dialog.configure(bg="#f7f9fc")
        dialog.grab_set()

        # Open as a reduced normal window while retaining the standard title
        # bar and Maximize/Restore control.  Do not make this a transient tool
        # window: Windows can remove the Maximize button from transient dialogs.

        outer = ttk.Frame(dialog, padding=18)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text="Change automatization parameters",
            bg="#f7f9fc",
            fg="#0b1220",
            font=("Segoe UI", 22, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            outer,
            text=(
                "Basic controls contain the run recipe, operator-facing control choices and top-level safety limits. "
                "Open Expert mode only for controller tuning, filters, signal-quality thresholds or low-level timing. "
                "Changes apply only to the current launcher session "
                "unless saved as a mode. COM ports, baud rates and fixed equipment hard stops remain locked."
            ),
            bg="#f7f9fc",
            fg="#334155",
            font=("Segoe UI", 10),
            justify="left",
            wraplength=910,
            anchor="w",
        ).pack(fill="x", pady=(6, 14))

        warning = tk.Frame(outer, bg="#fff7ed", highlightbackground="#fb923c", highlightthickness=1)
        warning.pack(fill="x", pady=(0, 12))
        tk.Label(
            warning,
            text=(
                "Experimental reminder: changing temperatures, timings, currents or PID gains changes the physical recipe. "
                "Review every edited value with the chamber operator before starting hardware."
            ),
            bg="#fff7ed",
            fg="#9a3412",
            font=("Segoe UI", 9, "bold"),
            justify="left",
            wraplength=900,
            anchor="w",
        ).pack(fill="x", padx=10, pady=8)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        editor_vars: dict[str, dict[str, object]] = {}
        tab_order: list[str] = []
        scroll_canvases: dict[str, object] = {}

        for phase in PHASES:
            tab_order.append(phase.key)
            tab = tk.Frame(notebook, bg=phase.card_bg)
            notebook.add(tab, text=f"{phase.number} {phase.title}")

            canvas = tk.Canvas(tab, bg=phase.card_bg, highlightthickness=0)
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            content = tk.Frame(canvas, bg=phase.card_bg)
            window_id = canvas.create_window((0, 0), window=content, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")
            scroll_canvases[phase.key] = canvas

            content.bind(
                "<Configure>",
                lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")),
            )
            canvas.bind(
                "<Configure>",
                lambda event, c=canvas, wid=window_id: c.itemconfigure(wid, width=event.width),
            )

            values = self.automation_parameter_values[phase.key]
            phase_vars: dict[str, object] = {}
            editor_vars[phase.key] = phase_vars
            phase_specs = list(specs_for_phase(phase.key))
            basic_keys = BASIC_PARAMETER_KEYS.get(phase.key, set())
            basic_specs = [spec for spec in phase_specs if spec.key in basic_keys]
            expert_specs = [spec for spec in phase_specs if spec.key not in basic_keys]

            def render_parameter_specs(parent, specs, *, start_row=0):
                row = start_row
                grouped: dict[str, list[object]] = {}
                for spec in specs:
                    grouped.setdefault(spec.group, []).append(spec)
                for group_name, group_specs in grouped.items():
                    tk.Label(
                        parent, text=group_name, bg=phase.accent, fg="#111827",
                        font=("Segoe UI", 11, "bold"), anchor="w", padx=10, pady=5,
                    ).grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(12 if row else 8, 5))
                    row += 1
                    for spec in group_specs:
                        tk.Label(
                            parent, text=spec.label, bg=phase.card_bg, fg="#111827",
                            font=("Segoe UI", 9, "bold"), anchor="w",
                        ).grid(row=row, column=0, sticky="nw", padx=(14, 8), pady=5)

                        if spec.kind == "bool":
                            var = tk.BooleanVar(value=bool(values[spec.key]))
                            widget = tk.Checkbutton(
                                parent, variable=var, bg=phase.card_bg,
                                activebackground=phase.card_bg, selectcolor="#ffffff",
                            )
                        elif spec.kind == "choice":
                            var = tk.StringVar(value=str(values[spec.key]))
                            widget = ttk.Combobox(parent, textvariable=var, values=spec.choices, state="readonly", width=18)
                        else:
                            var = tk.StringVar(value=spec.format_display(values[spec.key]))
                            widget = tk.Entry(
                                parent, textvariable=var, bg="#ffffff", fg="#111827",
                                insertbackground="#111827", relief="flat", highlightthickness=1,
                                highlightbackground="#94a3b8", highlightcolor=phase.accent,
                                font=("Segoe UI", 9), width=20,
                            )
                        phase_vars[spec.key] = var
                        widget.grid(row=row, column=1, sticky="ew", padx=4, pady=5)

                        tk.Label(
                            parent, text=spec.unit, bg=phase.card_bg, fg="#334155",
                            font=("Segoe UI", 9), anchor="w", width=10,
                        ).grid(row=row, column=2, sticky="nw", padx=6, pady=5)
                        tk.Label(
                            parent, text=spec.description, bg=phase.card_bg, fg="#475569",
                            font=("Segoe UI", 8), justify="left", wraplength=390, anchor="w",
                        ).grid(row=row, column=3, sticky="nw", padx=(8, 14), pady=5)
                        row += 1
                parent.columnconfigure(1, weight=0)
                parent.columnconfigure(3, weight=1)
                return row

            tk.Label(
                content, text="Basic controls", bg=phase.card_bg, fg="#0f172a",
                font=("Segoe UI", 13, "bold"), anchor="w",
            ).grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=(10, 0))
            tk.Label(
                content, text="Run recipe, control behaviour and safety limits you may reasonably review before a run.",
                bg=phase.card_bg, fg="#64748b", font=("Segoe UI", 9), anchor="w",
            ).grid(row=1, column=0, columnspan=4, sticky="ew", padx=12, pady=(2, 2))
            row = render_parameter_specs(content, basic_specs, start_row=2)

            expert_button = tk.Button(
                content, text="Expert mode  ▸", bg="#e2e8f0", fg="#0f172a",
                activebackground="#cbd5e1", relief="solid", bd=1, padx=12, pady=7,
                font=("Segoe UI", 9, "bold"), cursor="hand2", anchor="w",
            )
            expert_button.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(16, 6))
            row += 1
            expert_frame = tk.Frame(content, bg=phase.card_bg)
            expert_frame.grid(row=row, column=0, columnspan=4, sticky="ew")
            expert_frame.grid_remove()
            expert_open = {"value": False}

            def toggle_expert(frame=expert_frame, button=expert_button, state=expert_open, canvas=canvas, content=content):
                state["value"] = not state["value"]
                if state["value"]:
                    frame.grid()
                    button.configure(text="Expert mode  ▾")
                else:
                    frame.grid_remove()
                    button.configure(text="Expert mode  ▸")
                content.update_idletasks()
                canvas.configure(scrollregion=canvas.bbox("all"))

            expert_button.configure(command=toggle_expert)
            render_parameter_specs(expert_frame, expert_specs, start_row=0)
            content.columnconfigure(1, weight=0)
            content.columnconfigure(3, weight=1)

        # Shared pyrometer material mode. It is configured once in the launcher
        # and passed to Phases 01, 03 and 04 through a dedicated environment variable.
        pyro_bg = "#f3efff"
        pyro_accent = "#8b70d8"
        tab_order.append("pyrometer")
        pyro_tab = tk.Frame(notebook, bg=pyro_bg)
        notebook.add(pyro_tab, text="Pyrometer")

        pyro_canvas = tk.Canvas(pyro_tab, bg=pyro_bg, highlightthickness=0)
        pyro_scrollbar = ttk.Scrollbar(pyro_tab, orient="vertical", command=pyro_canvas.yview)
        pyro_content = tk.Frame(pyro_canvas, bg=pyro_bg)
        pyro_window_id = pyro_canvas.create_window((0, 0), window=pyro_content, anchor="nw")
        pyro_canvas.configure(yscrollcommand=pyro_scrollbar.set)
        pyro_canvas.pack(side="left", fill="both", expand=True)
        pyro_scrollbar.pack(side="right", fill="y")
        scroll_canvases["pyrometer"] = pyro_canvas
        pyro_content.bind(
            "<Configure>",
            lambda _event, c=pyro_canvas: c.configure(scrollregion=c.bbox("all")),
        )
        pyro_canvas.bind(
            "<Configure>",
            lambda event, c=pyro_canvas, wid=pyro_window_id: c.itemconfigure(wid, width=event.width),
        )

        pyro_values = self.pyrometer_parameter_values
        pyrometer_view_labels = {
            "oven": "Oven PID",
            "pyrometer": "Pyrometer raw",
            "sample": "Sample estimate",
        }
        pyrometer_view_values = {label: key for key, label in pyrometer_view_labels.items()}
        pyro_vars: dict[str, object] = {}
        editor_vars["pyrometer"] = pyro_vars
        profile_update_guard = {"active": False}
        current_group = None
        row = 0

        intro = tk.Frame(pyro_content, bg="#ffffff", highlightbackground="#c4b5fd", highlightthickness=1)
        intro.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(8, 4))
        tk.Label(
            intro,
            text="Configure globally, view locally",
            bg="#ffffff",
            fg="#4c1d95",
            font=("Segoe UI", 12, "bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(
            intro,
            text=(
                "Saved material modes keep emissivity and the matching sample-temperature calibration together. "
                "The selected mode is used by Phases 01, 03 and 04. The pyrometer remains monitoring-only and "
                "all temperature series are logged regardless of the live graph selection."
            ),
            bg="#ffffff",
            fg="#475569",
            font=("Segoe UI", 9),
            justify="left",
            wraplength=820,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 10))
        row += 1

        # Persistent mode selector and management controls.
        tk.Label(
            pyro_content,
            text="Saved material modes",
            bg=pyro_accent,
            fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
            padx=10,
            pady=5,
        ).grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(12, 5))
        row += 1

        tk.Label(
            pyro_content,
            text="Selected mode",
            bg=pyro_bg,
            fg="#111827",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(14, 8), pady=6)
        profile_var = tk.StringVar(value=str(pyro_values["profile_name"]))
        pyro_vars["profile_name"] = profile_var
        profile_combo = ttk.Combobox(
            pyro_content,
            textvariable=profile_var,
            values=tuple(self.pyrometer_profiles),
            state="readonly",
            width=30,
        )
        profile_combo.grid(row=row, column=1, sticky="ew", padx=4, pady=6)
        profile_buttons = tk.Frame(pyro_content, bg=pyro_bg)
        profile_buttons.grid(row=row, column=2, columnspan=2, sticky="w", padx=(8, 14), pady=6)
        profile_status_var = tk.StringVar(value=f"Saved in: {profile_store_path()}")
        row += 1
        tk.Label(
            pyro_content,
            textvariable=profile_status_var,
            bg=pyro_bg,
            fg="#64748b",
            font=("Segoe UI", 8),
            justify="left",
            anchor="w",
            wraplength=760,
        ).grid(row=row, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 7))
        row += 1

        for spec in pyrometer_specs():
            if spec.key == "profile_name":
                continue
            if spec.group != current_group:
                current_group = spec.group
                tk.Label(
                    pyro_content,
                    text=current_group,
                    bg=pyro_accent,
                    fg="#ffffff",
                    font=("Segoe UI", 11, "bold"),
                    anchor="w",
                    padx=10,
                    pady=5,
                ).grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(12, 5))
                row += 1

            tk.Label(
                pyro_content,
                text=spec.label,
                bg=pyro_bg,
                fg="#111827",
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).grid(row=row, column=0, sticky="nw", padx=(14, 8), pady=5)

            if spec.kind == "bool":
                var = tk.BooleanVar(value=bool(pyro_values[spec.key]))
                widget = tk.Checkbutton(
                    pyro_content,
                    variable=var,
                    bg=pyro_bg,
                    activebackground=pyro_bg,
                    selectcolor="#ffffff",
                )
            elif spec.kind == "choice":
                if spec.key == "default_view":
                    var = tk.StringVar(value=pyrometer_view_labels[str(pyro_values[spec.key])])
                    choice_values = tuple(pyrometer_view_labels[key] for key in spec.choices)
                else:
                    var = tk.StringVar(value=str(pyro_values[spec.key]))
                    choice_values = spec.choices
                widget = ttk.Combobox(
                    pyro_content,
                    textvariable=var,
                    values=choice_values,
                    state="readonly",
                    width=24,
                )
            else:
                var = tk.StringVar(value=spec.format_display(pyro_values[spec.key]))
                widget = tk.Entry(
                    pyro_content,
                    textvariable=var,
                    bg="#ffffff",
                    fg="#111827",
                    insertbackground="#111827",
                    relief="flat",
                    highlightthickness=1,
                    highlightbackground="#94a3b8",
                    highlightcolor=pyro_accent,
                    font=("Segoe UI", 9),
                    width=24,
                )
            pyro_vars[spec.key] = var
            widget.grid(row=row, column=1, sticky="ew", padx=4, pady=5)

            tk.Label(
                pyro_content,
                text=spec.unit,
                bg=pyro_bg,
                fg="#334155",
                font=("Segoe UI", 9),
                anchor="w",
                width=10,
            ).grid(row=row, column=2, sticky="nw", padx=6, pady=5)
            tk.Label(
                pyro_content,
                text=spec.description,
                bg=pyro_bg,
                fg="#475569",
                font=("Segoe UI", 8),
                justify="left",
                wraplength=390,
                anchor="w",
            ).grid(row=row, column=3, sticky="nw", padx=(8, 14), pady=5)
            row += 1

        pyro_content.columnconfigure(1, weight=0)
        pyro_content.columnconfigure(3, weight=1)

        def set_pyrometer_editor_values(values: dict[str, object]) -> None:
            profile_update_guard["active"] = True
            try:
                for spec in pyrometer_specs():
                    var = pyro_vars[spec.key]
                    if spec.kind == "bool":
                        var.set(bool(values[spec.key]))
                    elif spec.key == "default_view":
                        var.set(pyrometer_view_labels[str(values[spec.key])])
                    else:
                        var.set(spec.format_display(values[spec.key]))
            finally:
                profile_update_guard["active"] = False

        def read_pyrometer_editor_values() -> dict[str, object]:
            raw: dict[str, object] = {}
            for spec in pyrometer_specs():
                var = pyro_vars[spec.key]
                raw_value = var.get()
                if spec.key == "default_view":
                    raw_value = pyrometer_view_values[str(raw_value)]
                raw[spec.key] = spec.internal_value(raw_value)
            return validate_pyrometer_values(raw)

        def refresh_profile_choices(selected: str | None = None) -> None:
            self.pyrometer_profiles = load_pyrometer_profiles()
            choices = list(self.pyrometer_profiles)
            current = selected or profile_var.get()
            if current and current not in choices:
                choices.append(current)
            profile_combo.configure(values=tuple(choices))
            if current:
                profile_var.set(current)

        def load_selected_profile(_event=None) -> None:
            selected = profile_var.get()
            values = self.pyrometer_profiles.get(selected)
            if values is None:
                return
            set_pyrometer_editor_values(dict(values))
            profile_status_var.set(f"Loaded '{selected}'. Saved in: {profile_store_path()}")

        def mark_pyrometer_profile_custom(*_args) -> None:
            if profile_update_guard["active"]:
                return
            current = profile_var.get()
            if current in self.pyrometer_profiles:
                profile_update_guard["active"] = True
                try:
                    profile_var.set("Unsaved custom")
                    refresh_profile_choices("Unsaved custom")
                finally:
                    profile_update_guard["active"] = False
                profile_status_var.set("Current values have unsaved changes. Use Save mode to keep them for future runs.")

        for key in (
            "emissivity_percent",
            "sample_slope",
            "sample_intercept_c",
            "minimum_valid_pyrometer_c",
            "write_emissivity_at_start",
            "default_view",
        ):
            pyro_vars[key].trace_add("write", mark_pyrometer_profile_custom)

        def create_new_profile() -> None:
            name = self.simpledialog.askstring(
                "New pyrometer mode",
                "Name the material/calibration mode:",
                parent=dialog,
            )
            if not name or not name.strip():
                return
            clean = name.strip()
            if clean == VALIDATED_PROFILE_NAME:
                self.messagebox.showerror("Reserved name", "The validated Au/mica profile is read-only.", parent=dialog)
                return
            profile_update_guard["active"] = True
            try:
                profile_var.set(clean)
                refresh_profile_choices(clean)
            finally:
                profile_update_guard["active"] = False
            profile_status_var.set(f"New unsaved mode '{clean}'. Edit the values, then click Save mode.")

        def save_current_profile() -> None:
            try:
                values = read_pyrometer_editor_values()
            except Exception as exc:
                self.messagebox.showerror("Invalid pyrometer mode", str(exc), parent=dialog)
                return
            name = str(values["profile_name"]).strip()
            if name in {"", "Unsaved custom", VALIDATED_PROFILE_NAME}:
                name = self.simpledialog.askstring(
                    "Save pyrometer mode",
                    "Name this material/calibration mode:",
                    parent=dialog,
                ) or ""
                name = name.strip()
            if not name:
                return
            if name == VALIDATED_PROFILE_NAME:
                self.messagebox.showerror("Read-only mode", "The validated Au/mica profile cannot be overwritten.", parent=dialog)
                return
            if name in self.pyrometer_profiles:
                if not self.messagebox.askyesno("Replace saved mode", f"Replace the saved mode '{name}'?", parent=dialog):
                    return
            values["profile_name"] = name
            try:
                path = save_pyrometer_profile(name, values)
            except Exception as exc:
                self.messagebox.showerror("Could not save mode", str(exc), parent=dialog)
                return
            refresh_profile_choices(name)
            set_pyrometer_editor_values(dict(load_pyrometer_profiles()[name]))
            profile_status_var.set(f"Saved '{name}' in {path}")

        def delete_current_profile() -> None:
            name = profile_var.get().strip()
            if name in {VALIDATED_PROFILE_NAME, "Unsaved custom", ""}:
                self.messagebox.showinfo("Mode not deletable", "Select a saved custom mode first.", parent=dialog)
                return
            if not self.messagebox.askyesno("Delete pyrometer mode", f"Delete the saved mode '{name}'?", parent=dialog):
                return
            try:
                path = delete_pyrometer_profile(name)
            except Exception as exc:
                self.messagebox.showerror("Could not delete mode", str(exc), parent=dialog)
                return
            self.pyrometer_profiles = load_pyrometer_profiles()
            set_pyrometer_editor_values(dict(self.pyrometer_profiles[VALIDATED_PROFILE_NAME]))
            refresh_profile_choices(VALIDATED_PROFILE_NAME)
            profile_status_var.set(f"Deleted '{name}'. Profile file: {path}")

        profile_combo.bind("<<ComboboxSelected>>", load_selected_profile)
        for text, command, color in (
            ("New mode", create_new_profile, "#ddd6fe"),
            ("Save mode", save_current_profile, "#c4b5fd"),
            ("Delete", delete_current_profile, "#fee2e2"),
        ):
            tk.Button(
                profile_buttons,
                text=text,
                command=command,
                bg=color,
                fg="#111827",
                activebackground=color,
                relief="solid",
                bd=1,
                padx=9,
                pady=5,
                font=("Segoe UI", 8, "bold"),
            ).pack(side="left", padx=(0, 6))

        def set_phase_editor_values(phase_key: str, values: dict[str, object]) -> None:
            validated = validate_phase_values(phase_key, values)
            for spec in specs_for_phase(phase_key):
                var = editor_vars[phase_key][spec.key]
                if spec.kind == "bool":
                    var.set(bool(validated[spec.key]))
                else:
                    var.set(spec.format_display(validated[spec.key]))

        def read_all_editor_values() -> tuple[dict[str, dict[str, object]], dict[str, object]]:
            validated_by_phase: dict[str, dict[str, object]] = {}
            for phase_key in [phase.key for phase in PHASES]:
                raw: dict[str, object] = {}
                for spec in specs_for_phase(phase_key):
                    var = editor_vars[phase_key][spec.key]
                    raw[spec.key] = spec.internal_value(var.get())
                validated_by_phase[phase_key] = validate_phase_values(phase_key, raw)
            return validated_by_phase, read_pyrometer_editor_values()

        # Full-chamber saved automation modes. Loading a mode only fills the
        # editor; every field remains editable for the current launcher session.
        mode_bg = "#eef6ff"
        mode_accent = "#3b82f6"
        mode_tab = tk.Frame(notebook, bg=mode_bg)
        notebook.insert(0, mode_tab, text="Saved automation modes")
        tab_order.insert(0, "modes")
        mode_update_guard = {"active": False}

        mode_outer = tk.Frame(mode_tab, bg=mode_bg)
        mode_outer.pack(fill="both", expand=True, padx=18, pady=18)
        mode_intro = tk.Frame(mode_outer, bg="#ffffff", highlightbackground="#93c5fd", highlightthickness=1)
        mode_intro.pack(fill="x", pady=(0, 14))
        tk.Label(
            mode_intro,
            text="One-click chamber recipe setup",
            bg="#ffffff",
            fg="#1e3a8a",
            font=("Segoe UI", 15, "bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 3))
        tk.Label(
            mode_intro,
            text=(
                "A saved automation mode stores all editable startup parameters for Phases 01–04 "
                "together with the pyrometer material profile. Load a mode, review it, and press "
                "Apply for this launcher session. You can still change any individual field afterwards "
                "without modifying the saved mode."
            ),
            bg="#ffffff",
            fg="#475569",
            font=("Segoe UI", 10),
            justify="left",
            wraplength=850,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 12))

        selector_card = tk.Frame(mode_outer, bg="#dbeafe", highlightbackground="#93c5fd", highlightthickness=1)
        selector_card.pack(fill="x")
        tk.Label(
            selector_card, text="Saved chamber modes", bg=mode_accent, fg="#ffffff",
            font=("Segoe UI", 11, "bold"), anchor="w", padx=10, pady=6
        ).grid(row=0, column=0, columnspan=4, sticky="ew")
        selector_card.columnconfigure(1, weight=1)
        tk.Label(
            selector_card, text="Selected mode", bg="#dbeafe", fg="#111827",
            font=("Segoe UI", 9, "bold")
        ).grid(row=1, column=0, sticky="w", padx=(14, 8), pady=(12, 6))
        initial_mode_name = self.active_automation_mode_name
        initial_mode_choices = list(self.automation_modes)
        if initial_mode_name not in initial_mode_choices:
            initial_mode_choices.append(initial_mode_name)
        mode_var = tk.StringVar(value=initial_mode_name)
        mode_combo = ttk.Combobox(
            selector_card, textvariable=mode_var, values=tuple(initial_mode_choices),
            state="readonly", width=34
        )
        mode_combo.grid(row=1, column=1, sticky="ew", padx=4, pady=(12, 6))
        mode_buttons = tk.Frame(selector_card, bg="#dbeafe")
        mode_buttons.grid(row=1, column=2, columnspan=2, sticky="e", padx=(10, 14), pady=(12, 6))
        mode_description_var = tk.StringVar(
            value=self.automation_modes.get(initial_mode_name, {}).get(
                "description", "Run-specific values currently active in this launcher session."
            ) or "No description saved."
        )
        tk.Label(
            selector_card, textvariable=mode_description_var, bg="#dbeafe", fg="#334155",
            font=("Segoe UI", 9), justify="left", wraplength=820, anchor="w"
        ).grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=(2, 5))
        mode_status_var = tk.StringVar(value=f"Saved in: {mode_store_path()}")
        tk.Label(
            selector_card, textvariable=mode_status_var, bg="#dbeafe", fg="#64748b",
            font=("Segoe UI", 8), justify="left", wraplength=820, anchor="w"
        ).grid(row=3, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 12))

        def refresh_automation_mode_choices(selected: str | None = None) -> None:
            self.automation_modes = load_automation_modes()
            choices = list(self.automation_modes)
            current = selected or mode_var.get()
            if current and current not in choices:
                choices.append(current)
            mode_combo.configure(values=tuple(choices))
            if current:
                mode_var.set(current)
            saved = self.automation_modes.get(current)
            mode_description_var.set((saved or {}).get("description", "Unsaved run-specific settings."))

        def load_selected_automation_mode(_event=None) -> None:
            name = mode_var.get().strip()
            mode = self.automation_modes.get(name)
            if mode is None:
                return
            mode_update_guard["active"] = True
            try:
                validated = validate_automation_mode(mode)
                for phase_key, values in validated["phases"].items():
                    set_phase_editor_values(phase_key, values)
                set_pyrometer_editor_values(dict(validated["pyrometer"]))
                refresh_profile_choices(str(validated["pyrometer"]["profile_name"]))
            finally:
                mode_update_guard["active"] = False
            mode_description_var.set(validated["description"] or "No description saved.")
            mode_status_var.set(
                f"Loaded '{name}' into the editor. Review or adjust any tab, then click Apply. "
                f"File: {mode_store_path()}"
            )

        def current_editor_as_mode(description: str = "") -> dict[str, object]:
            phases, pyrometer = read_all_editor_values()
            return validate_automation_mode({
                "description": description,
                "phases": phases,
                "pyrometer": pyrometer,
            })

        def save_current_automation_mode() -> None:
            try:
                current = current_editor_as_mode()
            except Exception as exc:
                self.messagebox.showerror("Invalid automation mode", str(exc), parent=dialog)
                return
            proposed = mode_var.get().strip()
            if proposed in {"", PACKAGED_DEFAULT_MODE_NAME} or proposed in self.automation_modes:
                proposed = ""
            name = self.simpledialog.askstring(
                "Save automation mode",
                "Name this full chamber recipe (for example NPG at 600 C or GNR at 500 C):",
                initialvalue=proposed,
                parent=dialog,
            ) or ""
            name = name.strip()
            if not name:
                return
            if name == PACKAGED_DEFAULT_MODE_NAME:
                self.messagebox.showerror("Read-only mode", "Packaged defaults cannot be overwritten.", parent=dialog)
                return
            if name in self.automation_modes and not self.messagebox.askyesno(
                "Replace saved mode", f"Replace the saved automation mode '{name}'?", parent=dialog
            ):
                return
            description = self.simpledialog.askstring(
                "Mode description",
                "Optional short description for the next operator:",
                initialvalue=str(self.automation_modes.get(name, {}).get("description", "")),
                parent=dialog,
            )
            if description is None:
                return
            current["description"] = description.strip()
            try:
                path = save_automation_mode(name, current)
            except Exception as exc:
                self.messagebox.showerror("Could not save automation mode", str(exc), parent=dialog)
                return
            refresh_automation_mode_choices(name)
            mode_description_var.set(description.strip() or "No description saved.")
            mode_status_var.set(f"Saved full chamber mode '{name}' in {path}")

        def delete_current_automation_mode() -> None:
            name = mode_var.get().strip()
            if name in {"", PACKAGED_DEFAULT_MODE_NAME}:
                self.messagebox.showinfo("Mode not deletable", "Select a saved custom chamber mode first.", parent=dialog)
                return
            if not self.messagebox.askyesno(
                "Delete automation mode", f"Delete the saved full chamber mode '{name}'?", parent=dialog
            ):
                return
            try:
                path = delete_automation_mode(name)
            except Exception as exc:
                self.messagebox.showerror("Could not delete automation mode", str(exc), parent=dialog)
                return
            refresh_automation_mode_choices(PACKAGED_DEFAULT_MODE_NAME)
            mode_status_var.set(f"Deleted '{name}'. Mode file: {path}")

        def mark_full_mode_edited(*_args) -> None:
            if mode_update_guard["active"]:
                return
            mode_status_var.set(
                "The editor contains run-specific changes. Apply them for this session, or save them as a new mode."
            )

        for phase_key in [phase.key for phase in PHASES]:
            for var in editor_vars[phase_key].values():
                var.trace_add("write", mark_full_mode_edited)
        for var in editor_vars["pyrometer"].values():
            var.trace_add("write", mark_full_mode_edited)

        mode_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: (
                mode_description_var.set(
                    self.automation_modes.get(mode_var.get(), {}).get("description", "No description saved.")
                ),
                mode_status_var.set("Mode selected. Click Load mode to fill all parameter tabs."),
            ),
        )
        for text, command, color in (
            ("Load mode", load_selected_automation_mode, "#bfdbfe"),
            ("Save current as mode", save_current_automation_mode, "#a7f3d0"),
            ("Delete", delete_current_automation_mode, "#fecaca"),
        ):
            tk.Button(
                mode_buttons, text=text, command=command, bg=color, fg="#111827",
                activebackground=color, relief="solid", bd=1, padx=10, pady=6,
                font=("Segoe UI", 8, "bold")
            ).pack(side="left", padx=(0, 6))

        steps = tk.Frame(mode_outer, bg="#ffffff", highlightbackground="#cbd5e1", highlightthickness=1)
        steps.pack(fill="x", pady=(14, 0))
        tk.Label(
            steps, text="Recommended operator workflow", bg="#ffffff", fg="#0f172a",
            font=("Segoe UI", 11, "bold"), anchor="w"
        ).pack(fill="x", padx=14, pady=(11, 4))
        tk.Label(
            steps,
            text=(
                "1. Select a tutor-approved mode and click Load mode.   "
                "2. Review the phase tabs; change only what is needed for this run.   "
                "3. Click Apply. The source files and the saved mode remain unchanged.\n\n"
                "Not stored in modes: run names, the Phase 01 thickness ratio, COM ports, baud rates, "
                "or fixed equipment hard stops. The saved mode does include the approved Phase 01/03 watchdog maximum and automatic-current cap."
            ),
            bg="#ffffff", fg="#475569", font=("Segoe UI", 9), justify="left",
            wraplength=850, anchor="w"
        ).pack(fill="x", padx=14, pady=(0, 12))

        notebook.select(mode_tab)

        def scroll_active_tab(event) -> str | None:
            """Scroll the selected parameter tab with the mouse wheel."""

            phase_key = tab_order[notebook.index(notebook.select())]
            canvas = scroll_canvases.get(phase_key)
            if canvas is None:
                return None

            if getattr(event, "num", None) == 4:
                steps = -1
            elif getattr(event, "num", None) == 5:
                steps = 1
            else:
                delta = int(getattr(event, "delta", 0))
                if delta == 0:
                    return None
                steps = -int(delta / 120)
                if steps == 0:
                    steps = -1 if delta > 0 else 1

            canvas.yview_scroll(steps, "units")
            return "break"

        # The Toplevel bind tag receives wheel events from entries, labels and
        # the tab background, so the operator does not need to place the pointer
        # precisely over the scrollbar. Button-4/5 covers Linux Tk builds.
        dialog.bind("<MouseWheel>", scroll_active_tab, add="+")
        dialog.bind("<Button-4>", scroll_active_tab, add="+")
        dialog.bind("<Button-5>", scroll_active_tab, add="+")

        action_bar = ttk.Frame(outer)
        action_bar.pack(fill="x", pady=(12, 0))
        action_bar.columnconfigure(0, weight=1)

        def set_phase_to_defaults(phase_key: str) -> None:
            if phase_key == "modes":
                load_selected_automation_mode()
                return
            if phase_key == "pyrometer":
                defaults = pyrometer_default_values()
                set_pyrometer_editor_values(defaults)
                refresh_profile_choices(VALIDATED_PROFILE_NAME)
                profile_status_var.set("Validated Au/mica mode restored.")
                return
            set_phase_editor_values(phase_key, {spec.key: spec.default for spec in specs_for_phase(phase_key)})

        def reset_current_phase() -> None:
            phase_key = tab_order[notebook.index(notebook.select())]
            set_phase_to_defaults(phase_key)

        def reset_all_phases() -> None:
            mode_update_guard["active"] = True
            try:
                for phase_key in [phase.key for phase in PHASES]:
                    set_phase_to_defaults(phase_key)
                set_phase_to_defaults("pyrometer")
                refresh_automation_mode_choices(PACKAGED_DEFAULT_MODE_NAME)
                mode_description_var.set(
                    self.automation_modes[PACKAGED_DEFAULT_MODE_NAME]["description"]
                )
            finally:
                mode_update_guard["active"] = False
            mode_status_var.set("All editor tabs restored to packaged defaults.")

        def apply_changes() -> None:
            try:
                validated_by_phase, validated_pyrometer = read_all_editor_values()
            except Exception as exc:
                self.messagebox.showerror(
                    "Invalid automation parameter",
                    f"The changes were not applied.\n\n{exc}",
                    parent=dialog,
                )
                return

            previous_emissivity = float(self.pyrometer_parameter_values["emissivity_percent"])
            requested_emissivity = float(validated_pyrometer["emissivity_percent"])
            will_write_emissivity = bool(validated_pyrometer["write_emissivity_at_start"])
            if will_write_emissivity and abs(requested_emissivity - previous_emissivity) > 0.05:
                confirmed = self.messagebox.askyesno(
                    "Confirm pyrometer emissivity",
                    (
                        f"This launcher run will request pyrometer emissivity "
                        f"{requested_emissivity:.1f}% for profile "
                        f"'{validated_pyrometer['profile_name']}'.\n\n"
                        "At phase startup the instrument value will be read first, changed only "
                        "if it differs, and verified by readback. The calibration profile must "
                        "correspond to this emissivity.\n\nApply these settings?"
                    ),
                    parent=dialog,
                )
                if not confirmed:
                    return

            self.automation_parameter_values = validated_by_phase
            self.pyrometer_parameter_values = validated_pyrometer

            selected_mode_name = mode_var.get().strip()
            selected_mode = self.automation_modes.get(selected_mode_name)
            if selected_mode is not None:
                selected_validated = validate_automation_mode(selected_mode)
                exact_mode_match = (
                    selected_validated["phases"] == validated_by_phase
                    and selected_validated["pyrometer"] == validated_pyrometer
                )
            else:
                exact_mode_match = False
            self.active_automation_mode_name = (
                selected_mode_name if exact_mode_match else "Custom run settings"
            )

            changed_counts = {
                phase_key: len(non_default_overrides(phase_key, values))
                for phase_key, values in validated_by_phase.items()
            }
            pyrometer_changed = len(non_default_pyrometer_overrides(validated_pyrometer))
            total_changed = sum(changed_counts.values()) + pyrometer_changed
            mode_prefix = f"Automation mode: {self.active_automation_mode_name}. "
            if total_changed:
                summary_parts = [
                    f"{PHASE_BY_KEY[key].number}: {count}"
                    for key, count in changed_counts.items()
                    if count
                ]
                if pyrometer_changed:
                    summary_parts.append(f"Pyrometer: {pyrometer_changed}")
                self.status_var.set(
                    mode_prefix
                    + "Run-only parameters prepared ("
                    + ", ".join(summary_parts)
                    + "). Packaged source defaults remain unchanged."
                )
            else:
                self.status_var.set(
                    mode_prefix + "Automation and pyrometer parameters use the packaged defaults."
                )
            dialog.destroy()

        tk.Button(
            action_bar,
            text="Reset current phase",
            bg="#e2e8f0",
            fg="#111827",
            activebackground="#cbd5e1",
            relief="solid",
            bd=1,
            padx=12,
            pady=7,
            font=("Segoe UI", 9, "bold"),
            command=reset_current_phase,
        ).grid(row=0, column=1, padx=(0, 8))
        tk.Button(
            action_bar,
            text="Reset all",
            bg="#e2e8f0",
            fg="#111827",
            activebackground="#cbd5e1",
            relief="solid",
            bd=1,
            padx=12,
            pady=7,
            font=("Segoe UI", 9, "bold"),
            command=reset_all_phases,
        ).grid(row=0, column=2, padx=(0, 8))
        tk.Button(
            action_bar,
            text="Cancel",
            bg="#fca5a5",
            fg="#111827",
            activebackground="#f87171",
            relief="solid",
            bd=1,
            padx=14,
            pady=7,
            font=("Segoe UI", 9, "bold"),
            command=dialog.destroy,
        ).grid(row=0, column=3, padx=(0, 8))
        tk.Button(
            action_bar,
            text="Apply for this launcher run",
            bg="#c4b5fd",
            fg="#111827",
            activebackground="#a78bfa",
            relief="solid",
            bd=2,
            highlightthickness=1,
            highlightbackground="#111827",
            padx=16,
            pady=7,
            font=("Segoe UI", 9, "bold"),
            command=apply_changes,
        ).grid(row=0, column=4)

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.wait_window()

    def _readme_candidates(self) -> list[Path]:
        """Return likely READ ME paths for source/editable installations."""

        package_file = Path(__file__).resolve()
        return [
            Path.cwd() / "READ ME.md",
            package_file.parents[1] / "READ ME.md",
            package_file.parents[2] / "READ ME.md" if len(package_file.parents) > 2 else package_file.parents[1] / "READ ME.md",
        ]

    def _find_readme_path(self) -> Path | None:
        seen: set[Path] = set()
        for candidate in self._readme_candidates():
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return resolved
        return None

    def open_readme(self) -> None:
        """Open the unified SOP document with the operating system default app."""

        readme_path = self._find_readme_path()
        if readme_path is None:
            self.messagebox.showerror(
                "READ ME not found",
                "Could not find 'READ ME.md'. Make sure you are running the launcher from the project folder."
            )
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(readme_path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(readme_path)])
            else:
                subprocess.Popen(["xdg-open", str(readme_path)])
            self.status_var.set(f"Opened READ ME: {readme_path}")
        except Exception as exc:
            self.messagebox.showerror(
                "Could not open READ ME",
                f"The file exists, but the operating system could not open it.\n\n{readme_path}\n\nError: {exc}",
            )

    def _explanation_candidates(self, phase: PhaseInfo) -> list[Path]:
        """Return likely explanation-PDF paths for editable and wheel installations."""

        package_file = Path(__file__).resolve()
        return [
            Path.cwd() / "npg_chamber" / "script_explanations" / phase.explanation_file,
            package_file.parent / "script_explanations" / phase.explanation_file,
            package_file.parents[1] / "npg_chamber" / "script_explanations" / phase.explanation_file,
        ]

    def _find_explanation_path(self, key: str) -> Path | None:
        phase = PHASE_BY_KEY[key]
        seen: set[Path] = set()
        for candidate in self._explanation_candidates(phase):
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                return resolved
        return None

    def open_explanation(self, key: str) -> None:
        """Open the selected phase explanation PDF with the default PDF viewer."""

        phase = PHASE_BY_KEY[key]
        pdf_path = self._find_explanation_path(key)
        if pdf_path is None:
            self.messagebox.showerror(
                "Explanation PDF not found",
                f"Could not find the PDF explanation for {phase.number} {phase.title}.\n\n"
                "Make sure the package folder contains npg_chamber/script_explanations/.",
            )
            return

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(pdf_path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(pdf_path)])
            else:
                subprocess.Popen(["xdg-open", str(pdf_path)])
            self.status_var.set(f"Opened explanation: {phase.number} {phase.title}")
        except Exception as exc:
            self.messagebox.showerror(
                "Could not open explanation PDF",
                f"The file exists, but the operating system could not open it.\n\n{pdf_path}\n\nError: {exc}",
            )

    def _initial_env_for_phase(self, key: str) -> dict[str, str] | None:
        run_name = self.run_name_vars[key].get().strip()
        if not run_name:
            self.messagebox.showerror(
                "Run name required",
                f"Please enter a run name for {PHASE_BY_KEY[key].number} {PHASE_BY_KEY[key].title} before starting.",
            )
            return None

        env = {"NPG_CHAMBER_RUN_NAME": run_name}
        env[AUTOMATION_MODE_NAME_ENV] = self.active_automation_mode_name
        env[AUTOMATION_PARAMETERS_ENV] = encode_overrides(
            key,
            self.automation_parameter_values[key],
        )
        env[PYROMETER_PARAMETERS_ENV] = encode_pyrometer_settings(
            self.pyrometer_parameter_values
        )

        if key == "dpdbba":
            ratio_value = self._get_or_ask_thickness_ratio()
            if ratio_value is None:
                return None
            env["NPG_CHAMBER_THICKNESS_RATIO"] = self._format_ratio_for_env(ratio_value)

        return env

    @staticmethod
    def _format_ratio_for_env(value: float) -> str:
        return f"{float(value):.12g}"

    def _get_or_ask_thickness_ratio(self) -> float | None:
        """Confirm or collect the calibration ratio before every Phase 03 launch.

        A ratio recovered from Phase 01 is never used silently. The operator is
        shown the exact value and explicitly accepts it or chooses to replace it.
        If no ratio is available, Phase 03 keeps the existing manual-entry path.
        """

        existing = self.session_thickness_ratio
        if existing is not None and existing > 0:
            accepted = self.messagebox.askyesno(
                "Confirm thickness ratio",
                "Do you agree with the thickness ratio obtained?\n\n"
                f"Thickness ratio obtained in Phase 1: {existing:.6g}\n\n"
                "Yes: continue with this ratio.\n"
                "No: modify the ratio before starting Phase 03.",
                parent=self.root,
            )
            if accepted:
                return existing

            replacement = self._ask_manual_thickness_ratio(
                initial_value=existing,
                modifying=True,
            )
            if replacement is None:
                return None
            self.session_thickness_ratio = replacement
            self.session_ratio_source = "operator-modified value before Phase 03"
            self._update_ratio_status()
            return replacement

        ratio_value = self._ask_manual_thickness_ratio()
        if ratio_value is None:
            return None
        self.session_thickness_ratio = ratio_value
        self.session_ratio_source = "manual entry before Phase 03"
        self._update_ratio_status()
        return ratio_value

    def _ask_manual_thickness_ratio(
        self,
        *,
        initial_value: float | None = None,
        modifying: bool = False,
    ) -> float | None:
        """Ask for one positive ratio, retrying invalid input until cancel or success."""

        if modifying:
            prompt = (
                "Enter the thickness ratio to use for Phase 03.\n\n"
                "The value must be a positive number:"
            )
            title = "Modify thickness ratio"
        else:
            prompt = (
                "No Heat up + Calibration ratio is available in this launcher session.\n\n"
                "This usually means the launcher was restarted or Phase 01 was not run first.\n\n"
                "Enter the positive thickness ratio obtained in Heat up + Calibration:"
            )
            title = "Thickness ratio required"

        while True:
            kwargs = {"parent": self.root}
            if initial_value is not None:
                kwargs["initialvalue"] = self._format_ratio_for_env(initial_value)
            ratio_text = self.simpledialog.askstring(title, prompt, **kwargs)
            if ratio_text is None:
                return None

            ratio_text = ratio_text.strip().replace(",", ".")
            try:
                ratio_value = float(ratio_text)
            except (TypeError, ValueError):
                ratio_value = 0.0

            if ratio_value > 0:
                return ratio_value

            self.messagebox.showerror(
                "Invalid thickness ratio",
                "The DP-DBBA thickness ratio must be a positive number.",
                parent=self.root,
            )


    def _update_ratio_status(self) -> None:
        if self.session_thickness_ratio is None:
            self.dp_ratio_status_var.set("Phase 01 ratio will be confirmed before Phase 03")
            return
        source = self.session_ratio_source or "current launcher session"
        self.dp_ratio_status_var.set(
            f"Using ratio {self.session_thickness_ratio:.6g} from {source}"
        )

    def _parse_ratio_from_summary_file(self, path: Path) -> float | None:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        for line in text.splitlines():
            if line.lower().strip().startswith("thickness_ratio"):
                match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", line)
                if not match:
                    return None
                try:
                    value = float(match.group(0))
                except Exception:
                    return None
                return value if value > 0 else None
        return None

    def _extract_latest_heat_ratio(self) -> tuple[float, Path] | None:
        """Read the latest valid calibration ratio saved by Phase 01 in this session."""

        heat_parent = phase_data_dir("heat")
        run_name = self.run_name_vars.get("heat").get().strip() if "heat" in self.run_name_vars else ""
        safe_run = re.sub(r'[<>:"/\\|?*]+', "_", run_name).strip() or "unnamed_trial"
        preferred_name = f"Heat up + Calibration data {safe_run}"

        candidate_dirs = [p for p in heat_parent.iterdir() if p.is_dir()]
        preferred = [p for p in candidate_dirs if p.name == preferred_name]
        others = [p for p in candidate_dirs if p.name != preferred_name]
        candidate_dirs = sorted(preferred, key=lambda p: p.stat().st_mtime, reverse=True) + sorted(
            others,
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        summary_files: list[Path] = []
        for folder in candidate_dirs:
            summary_files.extend(folder.glob("*_summary.txt"))
        summary_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        for summary in summary_files:
            ratio = self._parse_ratio_from_summary_file(summary)
            if ratio is not None:
                return ratio, summary
        return None

    def _remember_heat_ratio_if_available(self) -> None:
        found = self._extract_latest_heat_ratio()
        if found is None:
            self.session_thickness_ratio = None
            self.session_ratio_source = None
            self._update_ratio_status()
            self.messagebox.showwarning(
                "Calibration ratio not found",
                "Phase 01 finished, but the launcher could not find a saved thickness_ratio "
                "in the Heat up + Calibration data folder.\n\n"
                "If you start DP-DBBA in this session, the launcher will ask you for the ratio manually.",
            )
            return

        ratio, summary_path = found
        self.session_thickness_ratio = ratio
        self.session_ratio_source = "Phase 01"
        self._update_ratio_status()
        self.status_var.set(f"Phase 01 ratio detected: {ratio:.6g}. Source: {summary_path.name}")

    def start_phase(self, key: str) -> None:
        if self.running_key is not None:
            self.messagebox.showinfo("Phase running", "A phase is already running.")
            return
        if key not in LEGACY_WORKFLOWS:
            self.messagebox.showerror("Unknown phase", f"Unknown phase: {key}")
            return

        initial_env = self._initial_env_for_phase(key)
        if initial_env is None:
            return

        workflow = LEGACY_WORKFLOWS[key]
        self.running_key = key
        self._set_buttons_enabled(False)
        self.status_var.set(
            f"Checking and resetting all chamber COM ports before {workflow.title}. Check CMD for details."
        )

        worker = threading.Thread(target=self._run_phase_worker, args=(key, initial_env), daemon=True)
        worker.start()

    def _run_phase_worker(self, key: str, extra_env: dict[str, str]) -> None:
        process: subprocess.Popen | None = None
        error_detail: str | None = None
        try:
            process = launch_legacy_workflow_process(key, extra_env=extra_env)
            with self.process_lock:
                self.current_process = process
            exit_code = wait_for_phase_process(process, key)
        except SerialHandoffError as exc:
            print(f"Serial handoff error for {key}:\n{exc}")
            exit_code = 90
            error_detail = str(exc)
        except Exception as exc:  # pragma: no cover - defensive GUI path
            print(f"Launcher error while running {key}: {exc}")
            exit_code = 1
            error_detail = str(exc)
        finally:
            with self.process_lock:
                if self.current_process is process:
                    self.current_process = None

        if not self.closing_requested:
            self.result_queue.put((key, int(exit_code), error_detail))

    def _poll_worker_results(self) -> None:
        try:
            key, exit_code, error_detail = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(250, self._poll_worker_results)
            return

        self.running_key = None
        self.last_completed_key = key
        self._set_buttons_enabled(True)

        phase = PHASE_BY_KEY[key]
        if exit_code == 0:
            self.status_var.set(
                f"Phase completed: {phase.number} {phase.title}. All chamber COM ports were reset and released."
            )
            if key == "heat":
                self._remember_heat_ratio_if_available()
            self._ask_next_phase(key)
        elif exit_code == 90:
            self.status_var.set(
                f"COM-port handoff failed after/before {phase.number} {phase.title}. The next phase was blocked."
            )
            detail = error_detail or (
                "One or more chamber COM ports could not be released. Check the CMD window for details."
            )
            self.messagebox.showerror(
                "COM ports not released",
                f"{detail}\n\nThe next phase has not been started. "
                "After closing any program that may be using a port, press Start again; "
                "the launcher will repeat the complete reset check automatically.",
            )
        else:
            self.status_var.set(f"The phase {phase.number} {phase.title} finished with code {exit_code}.")
            detail_text = f"\n\nDetails: {error_detail}" if error_detail else ""
            self.messagebox.showwarning(
                "Phase finished with warning",
                f"{phase.number} {phase.title} finished with code {exit_code}."
                f"{detail_text}\n\nCheck the CMD window and hardware status before continuing.",
            )

        self.root.after(250, self._poll_worker_results)

    def _ask_next_phase(self, key: str) -> None:
        next_key = NEXT_PHASE.get(key)
        if next_key is None:
            self.messagebox.showinfo(
                "Sequence completed",
                "This was the last phase. Check the saved data and chamber status.",
            )
            return

        next_phase = PHASE_BY_KEY[next_key]
        current_phase = PHASE_BY_KEY[key]
        go_next = self.messagebox.askyesno(
            "Continue to the next phase",
            f"{current_phase.number} {current_phase.title} has finished.\n\n"
            f"Do you want to start the next phase now?\n\n{next_phase.number} {next_phase.title}",
        )
        if go_next:
            self.start_phase(next_key)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.phase_buttons.values():
            button.configure(state=state)
        if self.parameter_button is not None:
            self.parameter_button.configure(state=state)

    def _phase_process_is_running(self) -> bool:
        with self.process_lock:
            process = self.current_process
        return process is not None and process.poll() is None

    def request_close(self) -> None:
        """Close the launcher without bypassing a phase hardware-safe shutdown.

        A running phase is a separate hardware-control process and owns its own
        Abort / Safe Stop implementation. Force-terminating that process from
        the launcher could skip ``finally``/``atexit`` cleanup and leave external
        equipment in an unknown state, so launcher close is intentionally
        blocked until the phase exits normally or completes its safe-stop path.
        """

        if self._phase_process_is_running() or self.running_key is not None:
            phase = PHASE_BY_KEY.get(self.running_key) if self.running_key else None
            phase_label = (
                f"{phase.number} {phase.title}" if phase is not None else "The current phase"
            )
            message = (
                f"{phase_label} is still running.\n\n"
                "For hardware safety, the unified launcher will not force-stop an active phase. "
                "Use Abort / Safe Stop inside the phase GUI and wait until that phase has fully "
                "finished and released the COM ports. Then close the launcher."
            )
            print("Launcher close blocked while a phase is active. Use the phase Abort / Safe Stop.")
            try:
                self.status_var.set("Close blocked: use the active phase Abort / Safe Stop first.")
            except Exception:
                pass
            try:
                self.messagebox.showwarning("Phase still running", message, parent=self.root)
            except Exception:
                pass
            return

        if self.closing_requested:
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        self.closing_requested = True
        try:
            self.status_var.set("Closing launcher ...")
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> int:
        self.root.mainloop()
        return 0


def launch_gui() -> int:
    """Open the graphical launcher, falling back with a helpful error if Tk fails."""

    app = NPGLauncherApp()
    return app.run()
