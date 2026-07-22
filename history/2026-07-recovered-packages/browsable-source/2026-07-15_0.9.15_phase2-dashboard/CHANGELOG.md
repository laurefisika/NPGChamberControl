# Changelog

## 0.9.15 - Compact white Phase 02 dashboard and honest Degas timing

- Reorganized the Phase 02 dashboard into a compact three-column desktop layout so the complete current-cycle workflow is permanently visible in the left column instead of appearing below the operator panel.
- Changed the dashboard to a white, high-readability background with subtle shadows and distinct pastel accents for workflow, guidance, timers, COSCON, oven and system-status cards.
- Changed all information-card headings to uppercase and refined spacing, font sizes and card density so the normal 1420 x 920 Phase 02 window fits without vertical scrolling. Smaller windows retain a responsive scrollable layout.
- Added a dedicated **SAFETY INTERLOCK** readback and an on-screen explanation: `OK` means the external hardware safety chain permits operation; `Tripped` means activation must be blocked or stopped.
- Removed the inherited 20-minute Degas guide estimate from the workflow and from the known-time estimate. Phase 02 still ends Degas only when COSCON naturally reports `Standby`.
- Renamed the active Degas countdown to **Degas safety timeout left**. The default 25-minute value is only the maximum allowed wait before aborting, not a prediction of how long Degas should take.
- Replaced the obsolete run-only parameter `expected_degassing_minutes` with `degas_timeout_minutes`, labelled **Degas safety timeout**.
- No COSCON commands, pressure limits, sputtering targets, oven recipe values, COM ports or safe-stop behavior were changed.

## 0.9.14 - Phase 02 UDP protocol fix and operator dashboard

- Corrected the COSCON UDP terminator in the integrated Phase 02 client. Version 0.9.13 sent the two literal characters `\r`; version 0.9.14 sends one real carriage-return byte, as required by the COSCON protocol.
- Corrected the integrated COSCON status and telemetry regular expressions, which had also been over-escaped. `GetStatus`, `GetMonitorValues`, `Mode=Error` and normal status replies are now parsed correctly.
- Removed the unused/blank COSCON web area from Phase 02. The COSCON web interface is not embedded and should remain closed while the automated phase is running.
- Rebuilt the Phase 02 window as a full operator dashboard with organized live telemetry for pressure, COSCON mode/interlock, energy, emission, filament current, oven PV/SV and Keysight readback.
- The dashboard creates only the buttons that are valid for the current prompt. Automated actions are shown as status/workflow steps instead of disabled buttons.
- The live PID SV control is shown only during the oven ramp and anneal hold, when that action is relevant.
- Added current-step countdown, current-stage elapsed time, total run elapsed time and an estimate of known timed work remaining. The estimate explicitly excludes manual valve waits and the variable oven-heating ramp.
- Added a visual workflow for the active cycle, including Degas, valve actions, pressure conditioning, COSCON activation, sputtering, automatic Standby, oven ramp, anneal hold and PID reset.
- Added countdowns for Degas timeout, pressure conditioning, COSCON activation/output qualification and automatic return to Standby.
- Improved transient monitor-error handling: resolved communication/read errors are cleared from the dashboard on the next fully successful monitor cycle.
- Extended `sputter_anneal_log.csv` with COSCON mode, interlock, details, energy, emission, filament current and live phase-countdown fields.
- Added the missing runtime `math` import used by the pressure validity guard.
- Preserved the original-script backup unchanged and kept all existing Phase 02 recipe values, pressure limits, COSCON target values, PID logic, data routing and serial-port handoff protection.

## 0.9.13 - Automated COSCON control in Phase 02

- Replaced the embedded/manual COSCON workflow in Phase 02 with direct, verified UDP control at `192.168.236.186:2005`.
- Added automated complete Degas for cycle 1, waiting for the device to finish naturally in `Standby`.
- Added automatic target validation and `SwitchToOperate` using the laboratory-tested target of 10 mA emission and 2250 V energy.
- Added measured-output qualification: Phase 02 requires five consecutive samples near 2250 V and 10 mA before starting the sputtering timer.
- Added continuous checks during sputtering for COSCON mode, interlock, measured energy, measured emission and chamber pressure.
- Added automatic `Operating -> Standby` transition before the operator is asked to close the manual argon leak valve.
- Added COSCON safe-stop handling on abort, device fault, pressure fault, communication failure and program shutdown. No automatic `Reset` command is used.
- Removed the need to click Degas started/finished, Sputter preset ready or Standby in the Phase 02 GUI. The corresponding controls are displayed as automated status items.
- The COSCON web interface is no longer used by Phase 02 and should remain closed while the phase runs.
- The argon leak valve remains an operator action because it has not yet been connected and validated as part of this chamber package.
- Preserved `original_scripts_backup/` unchanged as the recovery/reference copy. The authoritative automated runtime remains `npg_chamber/legacy_scripts/02_sputtering_annealing_legacy.py`.
- Kept the existing three-cycle sputtering/annealing recipe, oven PID control, data routing, run-only parameter system and COM-port handoff protection.

## 0.9.12 - Verified COM-port handoff between phases

### Original-script backup preservation (same 0.9.12 version)

- Restored `original_scripts_backup/` after review because a visible recovery/reference copy of the four phase scripts is valuable for laboratory maintenance.
- The restored files are the preserved pre-clean v0.9.12 copies, not regenerated files.
- The launcher and command-line workflows continue to execute only the authoritative scripts under `npg_chamber/legacy_scripts/`; the backup folder is never imported or launched automatically.
- `SOURCE_CODE_MANIFEST.json` now records independent hashes and sizes for both the active runtime scripts and the original backup copies.
- Added the backup folder to source-distribution packaging and regression tests so it is not removed accidentally in a future cleanup.
- Kept the package version and filename at `0.9.12`; no GUI, automation, serial handoff, experimental, or hardware behavior changed.

### Maintenance cleanup (same 0.9.12 version)

- Retained `original_scripts_backup/` intentionally as a recovery/reference artifact while keeping `npg_chamber/legacy_scripts/` as the only runtime source.
- Removed unused helper modules (`config/defaults.py`, `common/prompts.py`, and `common/timing.py`) that were not imported by the launcher, workflows, diagnostics, or phase scripts.
- Removed dead path/filename helpers and unused terminal logging functions.
- Removed one confirmed unused `dataclasses.field` import from Phase 02 and two unused imports from developer tests.
- Updated `READ ME.md`, source-manifest validation, and regression tests so dead helper files cannot be reintroduced and the intentional backup cannot be removed accidentally.
- Kept the package version at `0.9.12`; no automation values, phase logic, COM settings, safety limits, GUI behavior, or hardware commands were changed.

- Replaced the previous fixed-delay-only serial cleanup with an active COM-port handoff verification before and after every phase.
- The launcher now checks all six chamber ports (`COM4`, `COM16`, `COM6`, `COM9`, `COM17`, and `COM3`) before launching a phase.
- For each port, the launcher opens it without sending instrument commands, keeps hardware flow-control lines disabled, clears the PC-side input and output buffers, closes it, and confirms that Windows has released the handle.
- The same full check runs after the phase process exits. The launcher offers the next phase only after every chamber port has passed.
- Busy ports are retried automatically for up to 30 seconds. If one remains blocked, the next phase is prevented from starting and the GUI reports the exact COM port and Windows/serial error. Pressing Start again repeats the complete check, so a PC restart should no longer be the normal recovery procedure.
- Strengthened the internal shutdown of Phases 02 and 04 so their serial objects clear PC-side buffers before closing; Phases 01 and 03 retain their existing all-port cleanup and exit failsafes.
- Added developer tests for successful release, temporary `PermissionError(13)` recovery, permanent-port blocking, GUI integration, and phase-local cleanup.
- No experimental targets, phase-transition criteria, PID values, current/voltage safety limits, device commands, or synthesis logic were changed.

## 0.9.11 - Run-only automation parameter editor

- Added a new lavender **Change automatization parameters** button to the unified launcher, with the same black-outline visual style as the other footer buttons.
- Added a scrollable four-tab editor for the automation recipe of Phases 01-04. Operators can change phase targets, timings, ramp behaviour, PID gains and related workflow values before starting a phase.
- Parameter changes are validated and passed to the child phase through a phase-specific JSON environment variable; the Python source files and packaged defaults are never rewritten.
- Parameter selections exist only for the current launcher session. Closing and reopening the launcher restores all packaged defaults.
- COM ports, baud rates, plotting/logging settings and hard current/voltage safety limits are intentionally excluded from the editor.
- Added cross-parameter validation, including current-order checks, ramp-threshold checks, slope-order checks, pressure target/warning checks and annealing-stage order checks.
- Each phase now prints the received run-only overrides and saves an `automation_parameters.json` record containing the complete effective startup recipe for reproducibility.
- Added **Reset current phase** and **Reset all** controls inside the editor.
- Updated package documentation, tests, source manifest and version metadata.

## Serial cleanup hardening

- Added a launcher-side pause after each phase exits so Windows USB/COM drivers have time to release COM handles before the next phase starts.
- Added best-effort final serial cleanup to Phase 01 and Phase 03, explicitly closing every serial connection owned by the phase during normal finalization and Python-exit failsafe.
- Added a short serial-release pause after device shutdown in Phase 02 and Phase 04.
- No experimental values, PID values, phase-transition criteria, current limits, or synthesis logic were changed.

## 0.9.10 - Run names and closing of phase windows

- The run name entered in Phase 01 is now automatically copied to Phases 02, 03 and 04.
- Phase run names can still be edited manually at any time after being copied.
- Improved automatic closing of phase scripts launched from the unified GUI.
- Phase windows now close automatically after normal completion when launched from the unified interface.
- Improved shutdown handling when closing the GUI or pressing `CTRL+C` in the command window.
- Added stronger cleanup of child processes to avoid the unified GUI becoming blocked after a phase finishes.
- Updated the Sputtering-Annealing pressure handling so that `NaN` pressure readings from the XGS600 do not trigger a SPECS/pressure error.
- `NaN` pressure values are still displayed and logged, but they no longer stop the workflow.



## 0.9.9 - Start/Explanation button border and spacing refresh

- Added the same black border style to the Start and Explanation buttons.
- Made the black border around the Explanation buttons slightly thicker.
- Moved the Start and Explanation buttons slightly lower inside each phase card.
- Increased the vertical spacing between Start and Explanation buttons.
- No experimental script logic, parameters, COM ports, safety thresholds, or data routing were changed.

## 0.9.8 - Phase explanation PDF buttons

- Added an `Explanation` button under each phase Start button in the launcher GUI.
- Packaged the four PDF explanation documents inside `npg_chamber/script_explanations/`.
- The explanation buttons open the corresponding PDF with the operating system default PDF viewer.
- No changes were made to the four experimental scripts or their internal logic.


## 0.9.7 — High-contrast pastel GUI refresh

- Refined the launcher GUI for better readability on the light theme.
- Increased the main title size.
- Darkened the subtitle/status text for better contrast.
- Changed the four phase buttons to medium pastel colors:
  - Phase 01: pastel red.
  - Phase 02: pastel blue.
  - Phase 03: pastel yellow.
  - Phase 04: pastel green.
- Changed the phase cards to very light matching phase colors.
- Changed all phase card text and phase button text to black for consistent contrast.
- Added black outlines to the READ ME and Close buttons.
- No experimental script logic, parameters, COM ports, safety limits, or data routing were changed.

## 0.9.6 — Light GUI aesthetic refresh

- Updated the launcher GUI to use a clean light background.
- Changed Phase 01 accent to light red.
- Kept Phase 02 accent blue.
- Kept Phase 03 accent yellow.
- Changed Phase 04 accent to light green.
- Changed the READ ME button to green.
- Changed the Close button to red.
- No experimental script logic, variables, PID values, currents, timings, COM ports, or safety thresholds were changed.

## 0.9.5 — Close button shutdown and documentation refresh

- Updated the GUI **Close** button behavior:
  - if a phase is running, the launcher stops the running phase process before closing;
  - the GUI then closes normally;
  - `START_NPG_CHAMBER.bat` exits automatically on normal close without requiring an extra key press.
- Updated `READ ME.md` with the current GUI Close behavior and cleaner project-structure notes.
- Cleaned generated project artifacts from the release package (`__pycache__`, `.pytest_cache`, build folders, egg-info metadata, and internal wheel output).
- Kept the four experimental scripts intact; no PID values, currents, timings, ramps, ports, setpoints, or safety thresholds were changed.


## 0.9.4 — GUI startup-order fix

- Fixed the graphical launcher startup error: `Too early to create variable: no default root window`.
- The launcher now creates the Tk root window before creating any Tk variables.
- No run-name sanitization change was added in this version.
- No experimental script internals, control parameters, setpoints, safety thresholds, COM ports, or phase logic were changed.

## 0.9.3 — Automatic DP-DBBA ratio handoff

- Removed the always-visible DP-DBBA thickness-ratio input field from the launcher.
- After Phase 01 finishes, the launcher now reads the saved `thickness_ratio` from the Heat up + Calibration summary file and stores it for the current GUI session.
- When Phase 03 starts in the same session, the launcher passes that ratio automatically to the DP-DBBA script.
- If the launcher was restarted, or if Phase 01 was not run first, the launcher asks for the positive thickness ratio with a small pop-up only when DP-DBBA is started.
- When continuing from one phase to the next, the launcher pre-fills the next phase run name if it is blank.
- No experimental script internals, control parameters, setpoints, safety thresholds, COM ports, or phase logic were changed.

## 0.9.2 — GUI aesthetic refresh and startup input fields

- Enlarged and centered the **NPG Chamber Controller** GUI title.
- Put each phase number and phase name on the same line, for example `01 Heat up + Calibration`.
- Updated launcher phase colors:
  - `01` Heat up + Calibration: red
  - `02` Sputtering-Annealing: blue
  - `03` DP-DBBA Evaporation: yellow
  - `04` NPG Annealings: green
- Changed the **READ ME** button to pink and the **Close** button to orange.
- Added GUI startup fields for each phase run name.
- Added a GUI startup field for the DP-DBBA thickness ratio.
- The launcher passes those startup values to the scripts through environment variables, reducing the need to type startup values in CMD.
- Experimental control logic, setpoints, ramps, PID constants, safety thresholds, COM ports, and phase sequences were not changed.

## 0.9.1

- Added `START_NPG_CHAMBER.bat` for one-click Windows startup.
- The batch launcher creates `.venv` if needed, installs the package on first run, refreshes editable installation after updates, and opens the GUI.
- Updated the SOP to explain daily startup without manually typing `.venv\Scripts\activate`.
- No experimental script internals were changed.


## 0.9.0 — Centralized Data Samples output folders

- Added a project-level `Data Samples/` folder.
- Added four dedicated data parent folders:
  - `Data Samples/Heat up + Calibration Data/`
  - `Data Samples/Sputtering-Annealing Data/`
  - `Data Samples/DP-DBBA Evaporation Data/`
  - `Data Samples/NPG Annealing Data/`
- Updated the launcher so each phase starts with the matching data folder as its working/output context.
- Updated the scripts only where needed to route saved run folders into `Data Samples`.
- No experimental control parameters, ramps, PID values, setpoints, COM ports, safety thresholds, or phase logic were changed.

## 0.8.9 — SOP READ ME and GUI help button

- Added a **READ ME** button to the graphical launcher.
- Consolidated user instructions, installation steps, workflow description, commands, validation notes, troubleshooting and SOP content into `READ ME.md`.
- Removed duplicated documentation files that repeated the same instructions.
- Kept non-duplicated support files such as `CHANGELOG.md`, `LICENSE.md`, and `SOURCE_CODE_MANIFEST.json`.
- Preserved the four final experimental scripts without changing their internal logic, variables or parameters.


## 0.8.8 — Markdown-only documentation cleanup

- Removed duplicated `.txt` documentation files.
- Kept the canonical Markdown documents (`.md`) only.
- Updated project structure notes so they no longer mention duplicated `.txt` files.
- No experimental script logic, variables, parameters, setpoints, ramp timings, or safety behaviour were changed.

## 0.8.7 — English clean release

- User-facing documentation changed to English.
- GUI launcher text changed to English.
- User-facing folders renamed for clarity:
  - `original_scripts_backup/`: visible backup copies of the four final scripts.
  - `diagnostic_tools/`: manual hardware checks.
  - `developer_tests/`: safe tests that do not talk to hardware.
  - `maintenance_tools/`: general package checks.
  - `installation_notes/`: installation/build notes.
- Generated files were removed from the project ZIP:
  - `.pytest_cache/`
  - old `dist/` contents
  - build metadata folders
- The four final experimental scripts remain unchanged internally.

## 0.8.6 — GUI launcher

- Added graphical launcher opened by `npg-chamber`.
- The GUI has buttons for the four phases.
- After a phase ends successfully, the GUI asks whether to continue to the next phase.

## 0.8.5 — Final source verification

- Packaged the final four scripts supplied by the user:
  - `1. Heat up + Calibration_NEW TRY_v7.3.py`
  - `2. Sputtering-Annealing_NEW TRY_v4.py`
  - `3. DP-DBBA Evaporation_v7.py`
  - `4. NPG Annealings_NEW TRY_v3.py`
- Added `SOURCE_CODE_MANIFEST.json` with SHA256 hashes.
- The workflow wrappers now launch the exact packaged scripts for maximum safety.

## Earlier work

Earlier phases created the package, added the command-line launcher, added diagnostic device modules, added tests, and explored modular workflow migration. The final release keeps the internal experimental logic in the four scripts intact.