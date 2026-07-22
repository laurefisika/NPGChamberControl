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

from npg_chamber import __version__
from npg_chamber.common.paths import phase_data_dir
from npg_chamber.config.run_parameters import (
    AUTOMATION_PARAMETERS_ENV,
    all_default_values,
    encode_overrides,
    non_default_overrides,
    specs_for_phase,
    validate_phase_values,
)
from npg_chamber.workflows.legacy_runner import (
    LEGACY_WORKFLOWS,
    launch_legacy_workflow_process,
    terminate_process,
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
        self.root.title(f"NPG Chamber Controller · v{__version__}")

        self.result_queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self.running_key: str | None = None
        self.current_process: subprocess.Popen | None = None
        self.process_lock = threading.Lock()
        self.closing_requested = False
        self.last_completed_key: str | None = None
        self.run_name_vars: dict[str, tk.StringVar] = {}
        self._copying_phase1_run_name = False
        self.session_thickness_ratio: float | None = None
        self.session_ratio_source: str | None = None
        self.dp_ratio_status_var = tk.StringVar(value="Automatic after Phase 01; asked only if needed")
        self.automation_parameter_values = all_default_values()
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
            print("Close requested from CMD signal. Stopping launcher and running phase ...")
            try:
                self.root.after(0, self.request_close)
            except Exception:
                self.closing_requested = True
                self._terminate_running_phase()

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
            text="Tip: Close stops any running phase and exits the launcher. CMD is still used for logs.",
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
        dialog.geometry("980x760")
        dialog.minsize(820, 620)
        dialog.configure(bg="#f7f9fc")
        dialog.transient(self.root)
        dialog.grab_set()

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
                "These values are passed only to the selected phase process. They do not rewrite the Python files "
                "and return to the packaged defaults when the launcher is closed. COM ports, device settings, "
                "plotting/logging options and hard safety limits are intentionally not editable here."
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
            current_group: str | None = None
            row = 0

            for spec in specs_for_phase(phase.key):
                if spec.group != current_group:
                    current_group = spec.group
                    tk.Label(
                        content,
                        text=current_group,
                        bg=phase.accent,
                        fg="#111827",
                        font=("Segoe UI", 11, "bold"),
                        anchor="w",
                        padx=10,
                        pady=5,
                    ).grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(12 if row else 8, 5))
                    row += 1

                tk.Label(
                    content,
                    text=spec.label,
                    bg=phase.card_bg,
                    fg="#111827",
                    font=("Segoe UI", 9, "bold"),
                    anchor="w",
                ).grid(row=row, column=0, sticky="nw", padx=(14, 8), pady=5)

                if spec.kind == "bool":
                    var = tk.BooleanVar(value=bool(values[spec.key]))
                    widget = tk.Checkbutton(
                        content,
                        variable=var,
                        bg=phase.card_bg,
                        activebackground=phase.card_bg,
                        selectcolor="#ffffff",
                    )
                elif spec.kind == "choice":
                    var = tk.StringVar(value=str(values[spec.key]))
                    widget = ttk.Combobox(content, textvariable=var, values=spec.choices, state="readonly", width=18)
                else:
                    var = tk.StringVar(value=spec.format_display(values[spec.key]))
                    widget = tk.Entry(
                        content,
                        textvariable=var,
                        bg="#ffffff",
                        fg="#111827",
                        insertbackground="#111827",
                        relief="flat",
                        highlightthickness=1,
                        highlightbackground="#94a3b8",
                        highlightcolor=phase.accent,
                        font=("Segoe UI", 9),
                        width=20,
                    )
                phase_vars[spec.key] = var
                widget.grid(row=row, column=1, sticky="ew", padx=4, pady=5)

                tk.Label(
                    content,
                    text=spec.unit,
                    bg=phase.card_bg,
                    fg="#334155",
                    font=("Segoe UI", 9),
                    anchor="w",
                    width=10,
                ).grid(row=row, column=2, sticky="nw", padx=6, pady=5)
                tk.Label(
                    content,
                    text=spec.description,
                    bg=phase.card_bg,
                    fg="#475569",
                    font=("Segoe UI", 8),
                    justify="left",
                    wraplength=390,
                    anchor="w",
                ).grid(row=row, column=3, sticky="nw", padx=(8, 14), pady=5)
                row += 1

            content.columnconfigure(1, weight=0)
            content.columnconfigure(3, weight=1)

        action_bar = ttk.Frame(outer)
        action_bar.pack(fill="x", pady=(12, 0))
        action_bar.columnconfigure(0, weight=1)

        def set_phase_to_defaults(phase_key: str) -> None:
            for spec in specs_for_phase(phase_key):
                var = editor_vars[phase_key][spec.key]
                if spec.kind == "bool":
                    var.set(bool(spec.default))
                else:
                    var.set(spec.format_display(spec.default))

        def reset_current_phase() -> None:
            phase_key = tab_order[notebook.index(notebook.select())]
            set_phase_to_defaults(phase_key)

        def reset_all_phases() -> None:
            for phase_key in tab_order:
                set_phase_to_defaults(phase_key)

        def apply_changes() -> None:
            validated_by_phase: dict[str, dict[str, object]] = {}
            try:
                for phase_key in tab_order:
                    raw: dict[str, object] = {}
                    for spec in specs_for_phase(phase_key):
                        var = editor_vars[phase_key][spec.key]
                        value = var.get()
                        raw[spec.key] = spec.internal_value(value)
                    validated_by_phase[phase_key] = validate_phase_values(phase_key, raw)
            except Exception as exc:
                self.messagebox.showerror(
                    "Invalid automation parameter",
                    f"The changes were not applied.\n\n{exc}",
                    parent=dialog,
                )
                return

            self.automation_parameter_values = validated_by_phase
            changed_counts = {
                phase_key: len(non_default_overrides(phase_key, values))
                for phase_key, values in validated_by_phase.items()
            }
            total_changed = sum(changed_counts.values())
            if total_changed:
                summary = ", ".join(
                    f"{PHASE_BY_KEY[key].number}: {count}"
                    for key, count in changed_counts.items()
                    if count
                )
                self.status_var.set(
                    f"Run-only automation parameters prepared ({summary}). Packaged source defaults remain unchanged."
                )
            else:
                self.status_var.set("Automation parameters reset to the packaged defaults.")
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
        env[AUTOMATION_PARAMETERS_ENV] = encode_overrides(
            key,
            self.automation_parameter_values[key],
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
        """Return the Phase-1 ratio for DP-DBBA, asking only if this session lacks it."""

        if self.session_thickness_ratio is not None and self.session_thickness_ratio > 0:
            return self.session_thickness_ratio

        ratio_text = self.simpledialog.askstring(
            "Thickness ratio required",
            "No Heat up + Calibration ratio is available in this launcher session.\n\n"
            "This usually means the launcher was restarted or Phase 01 was not run first.\n\n"
            "Enter the positive thickness ratio obtained in Heat up + Calibration:",
            parent=self.root,
        )
        if ratio_text is None:
            return None

        ratio_text = ratio_text.strip().replace(",", ".")
        try:
            ratio_value = float(ratio_text)
        except Exception:
            self.messagebox.showerror(
                "Invalid thickness ratio",
                "The DP-DBBA thickness ratio must be a positive number.",
            )
            return None

        if ratio_value <= 0:
            self.messagebox.showerror(
                "Invalid thickness ratio",
                "The DP-DBBA thickness ratio must be a positive number.",
            )
            return None

        self.session_thickness_ratio = ratio_value
        self.session_ratio_source = "manual entry after launcher restart"
        self._update_ratio_status()
        return ratio_value

    def _update_ratio_status(self) -> None:
        if self.session_thickness_ratio is None:
            self.dp_ratio_status_var.set("Automatic after Phase 01; asked only if needed")
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
            f"Running: {workflow.title}. Startup values were provided by the GUI. Check CMD for logs."
        )

        worker = threading.Thread(target=self._run_phase_worker, args=(key, initial_env), daemon=True)
        worker.start()

    def _run_phase_worker(self, key: str, extra_env: dict[str, str]) -> None:
        process: subprocess.Popen | None = None
        try:
            process = launch_legacy_workflow_process(key, extra_env=extra_env)
            with self.process_lock:
                self.current_process = process
            exit_code = wait_for_phase_process(process, key)
        except Exception as exc:  # pragma: no cover - defensive GUI path
            print(f"Launcher error while running {key}: {exc}")
            exit_code = 1
        finally:
            with self.process_lock:
                if self.current_process is process:
                    self.current_process = None

        if not self.closing_requested:
            self.result_queue.put((key, int(exit_code)))

    def _poll_worker_results(self) -> None:
        try:
            key, exit_code = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(250, self._poll_worker_results)
            return

        self.running_key = None
        self.last_completed_key = key
        self._set_buttons_enabled(True)

        phase = PHASE_BY_KEY[key]
        if exit_code == 0:
            self.status_var.set(f"Phase completed: {phase.number} {phase.title}.")
            if key == "heat":
                self._remember_heat_ratio_if_available()
            self._ask_next_phase(key)
        else:
            self.status_var.set(f"The phase {phase.number} {phase.title} finished with code {exit_code}.")
            self.messagebox.showwarning(
                "Phase finished with warning",
                f"{phase.number} {phase.title} finished with code {exit_code}.\n\n"
                "Check the CMD window and hardware status before continuing.",
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

    def _terminate_running_phase(self) -> None:
        with self.process_lock:
            process = self.current_process
            self.current_process = None

        if process is not None and process.poll() is None:
            print("Close requested: stopping the running phase process ...")
            terminate_process(process)

    def request_close(self) -> None:
        """Close the GUI and stop any running phase process."""

        if self.closing_requested and self.current_process is None:
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        self.closing_requested = True
        try:
            self.status_var.set("Closing launcher and stopping any running phase ...")
        except Exception:
            pass
        self._terminate_running_phase()
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
