# Changelog

## Repository publication correction · 2026-08-18

- Re-published the complete `0.9.36` / `2026.08.11-r15` source tree from `npg_chamber_project_v17(9).zip` after the previous GitHub pull request changed only the package build marker.
- Included the four current phase scripts, the shared professional-control and Qt/PyQtGraph modules, launcher and installation logic, complete developer test suite, and all four explanation PDFs under `npg_chamber/script_explanations/`.
- Removed obsolete duplicate active documentation and runtime backup folders while preserving the repository history and empty `Data Samples` directory structure.
- Excluded machine-specific/runtime artifacts (`.venv`, `*.egg-info`, caches, macOS metadata, and experimental run outputs) from the release commit.
- Updated `CITATION.cff` to release `0.9.36` and corrected the source-distribution manifest pattern for `READ ME.md`.
- Restored the complete historical changelog from the v16 archive so the repository documents the full project evolution from v0.8.5 through v17.
- Reworked the four phase explanation PDFs as concise, color-coded one-page operator guides and streamlined repetitive or outdated SOP text.
- Added visible project credits for Roger Simon de Febrer and Piotr Krzysztof Ciochon, plus CC BY 4.0 licensing for the original project documentation.
- No chamber-control behavior was changed relative to the supplied v17(9) release source.

## Release history at a glance

| Version range | Main project milestone |
| --- | --- |
| 0.9.33-0.9.36 | v17 production hardening, minimal shutter gates, phase-local Abort/Finish semantics and current explanation PDFs |
| 0.9.23-0.9.32 | PySide6/PyQtGraph Phase 01/03 migration and professional temperature/rate/compound control |
| 0.9.22 | v16 editable safety limits and Phase 02 continuation-runtime correction |
| 0.9.21 | v15 CK-1 rate and compound feedback control |
| 0.9.20 | v14 PID safe-zero finalization |
| 0.9.13-0.9.19 | Direct COSCON automation, operator dashboards, full-chamber modes and responsive Phase 01/03 interfaces |
| 0.9.8-0.9.12 | Explanation PDFs, run naming, automation parameters and verified COM-port handoff |
| 0.8.5-0.9.7 | Initial packaging, unified launcher, centralized data folders and GUI presentation |

## 0.9.36 · build 2026.08.11-r15 · phase-local Abort / normal Finish semantics

- Phase 01 `Abort / safe stop` now prioritizes immediate Keysight `CURR 0.000` + `OUTP OFF`, then closes only the Phase 01 process; the unified launcher remains open.
- Phase 01 `Finish Phase` remains the normal controlled completion path: enter `RAMP_DOWN`, reach 0 A, switch output OFF, save, and close the phase.
- Phase 03 `Abort / safe stop` now immediately commands Keysight `CURR 0.000` + `OUTP OFF`; Oven PID `0 °C` is requested afterwards on a best-effort basis so PID communication cannot delay evaporator shutdown. The Phase 03 process then closes while the unified launcher remains open.
- Phase 03 `Finish Phase` preserves the established normal handoff: after target/shutter-close confirmation, return to `0.640 A`, keep `OUTPUT ON`, and close Phase 03 ready for Phase 04.
- Updated regression coverage and phase explanation PDFs for the new button semantics.

## 0.9.35 · build 2026.08.11-r14 · hardware-safe launcher close

- Final production-style safety review identified and removed the unified launcher's force-kill path for active phase processes.
- The launcher Close button, window X and launcher-level Ctrl+C now refuse to close while a phase is active and direct the operator to the phase's own `Abort / Safe Stop` path. This avoids bypassing phase `finally`/`atexit` hardware cleanup with `taskkill /F`.
- Removed the now-unused generic `terminate_process()` hard-kill helper from the workflow runner.
- Re-ran the full regression suite, source guard, syntax compilation and clean-ZIP verification after the change.

## 0.9.34 · build 2026.08.10-r13 · readiness cleanup and documentation refresh

- Removed the obsolete Phase 01/03 shutter-readiness diagnostic layer entirely: no hidden stable-read counter, no 120 s in-band readiness timer, no shutter-specific rate-trend threshold and no current-headroom readiness check.
- Kept the experimental shutter gate intentionally minimal: Phase 01 requires CK-1 temperature >= target and averaged CK-1 rate >= target; Phase 03 requires the same CK-1 criteria plus its real external-oven stability prerequisite.
- Renamed the one temperature-slope threshold still needed by Compound cascade control to `CASCADE_INNER_MAX_ABS_TEMP_SLOPE_C_PER_MIN`, making clear that it is controller-internal and never a shutter condition.
- Simplified the Phase 01/03 live status by removing obsolete readiness diagnostic rows and renaming `Rate band` to `Control rate band`.
- Refreshed all four packaged GUI/script explanation PDFs against the current unified project and build.
- Preserved safety watchdogs, QMB plausibility guards, Keysight current/voltage protections, PID/Compound control and Phase 03 oven qualification.

## Build 2026.08.10-r12

- Fixed Windows launcher false-negative runtime verification: startup no longer initializes the Phase 02 pywebview WinForms/.NET backend before opening the main launcher.
- Runtime health check now verifies dependency availability with `importlib.util.find_spec()` and leaves backend initialization to the phase that actually needs it.
- This prevents the repair -> verify failed loop seen even after a successful editable install.
- Distribution ZIP no longer ships a machine-specific `.venv`; an existing local `.venv` is reused, or a fresh one is created on first use.

## 0.9.33 · build 2026.08.10-r11 · Phase 01/03 shutter-readiness hotfix

- Fixed a v17 regression that could leave Phase 01 and Phase 03 trapped in `HEATING_UP` even after the operator-facing CK-1 temperature and average deposition-rate targets had been reached.
- Restored process-level shutter readiness to the documented criteria: CK-1 temperature at/above target and average CK-1 rate at/above target. Controller equilibrium diagnostics (temperature slope, rate trend, current headroom, rate-band qualification) remain available for diagnostics but no longer silently redefine the workflow transition.
- Phase 03 keeps its independent external-oven stability prerequisite before shutter opening.
- Added explicit Phase 03 thickness-ratio confirmation: a recovered Phase 01 ratio is shown to the operator with Yes/No confirmation; No opens an editable value, while missing ratios retain the manual-entry workflow.

## 0.9.33 · build 2026.08.07-r10

### Phase 02 COSCON activation robustness
- Treats only `HV-Module Energy Overload` during the pre-sputter COSCON activation as a recoverable transient; arbitrary COSCON errors remain fatal.
- On the first activation overload with Interlock OK, immediately requests and verifies a safe inactive COSCON state, waits 8 s while rechecking pressure/interlock, re-validates the requested target, and performs one automatic Operate retry.
- A repeated overload aborts safely instead of looping retries.
- Extended pre-sputter output qualification from 20 s to 30 s while COSCON remains in Operating, without relaxing energy/emission tolerances.
- Added expert automation parameters for activation-overload retry count and recovery wait.

## 0.9.33 · build 2026.08.07-r9

- Phase 01/03 live target fields now behave as one staged form: Temperature target, Rate target and PID band can all be edited before applying.
- Telemetry refresh no longer restores a live field to the active controller value merely because the operator clicks or tabs into another control.
- Unsaved target edits remain visible until `Apply targets`/Enter or `Reset targets`; the Apply button shows the number of staged changes.
- Submitted edits remain protected from stale telemetry until the live controller reports the requested values; newer edits typed while a command is in flight are preserved.


## 0.9.33 - build 2026.08.07-r7

- Phase 01 and Phase 03 live plot y-axes now display the measured values directly, with PyQtGraph automatic SI multipliers disabled (no `x0.0001`, `x10^-4`, etc.). Adaptive live autoscaling remains active.
- Reordered the Phase 01/03 right panel to: Operator controls; Editable targets and controller; Temperature graph selector; Live process status; GUI runtime health.
- Operator controls and Editable targets and controller now start expanded; the selector, live status and runtime-health sections start collapsed to reduce visual clutter.
- Extended source verification and regression coverage for the new axis-format and panel-order requirements.

## 0.9.33 - build 2026.08.07-r6

- Replaced the fragile inline `python -c` source guard in `START_NPG_CHAMBER.bat` with a dedicated `npg_chamber.installation_check` module.
- Fixed a Windows `cmd.exe` quoting bug that could falsely report `ERROR: The source files do not match build 2026.08.07-r5` even when the r5 files were correct.
- Source verification now reports individual `[OK]` / `[FAIL]` checks for build identity, active editable-project link, Phase 01 hotfix, Qt dashboard, automation-parameter UX, and Phase 04 finalization.
- The verifier remains read-only and does not import legacy phase scripts, so it cannot initialize chamber hardware.


## 0.9.33 · build 2026.08.07-r5
- Improved Phase 01/03 operator UX: Operator controls are now the first expandable card and the right control panel is wider.
- Added adaptive live plot fitting for Phase 01/03. The retained live window follows incoming data automatically, Y ranges use hysteresis to avoid jitter, and Oven/Pyrometer/Sample view changes force an immediate correct rescale.
- Promoted operator-facing Keysight ramp controls to Basic mode: ramp strategy, base working current, current step, step period/transition and safe ramp-down step/period. Phase 04 also exposes its ramp-down step, period and first-step delay in Basic mode. Low-level startup constants, estimator tuning and controller internals remain in Expert mode.
- Kept the four phase interfaces visually aligned around the same light surfaces, phase/status hierarchy, concise operator actions and restrained pastel accents while preserving each phase's workflow-specific controls.

## 0.9.33 — v17 professional cleanup

- Build `2026.08.07-r4`: added an explicit source-build banner and launcher preflight that refuses to start if the active Phase 01 / Qt dashboard files do not contain the current hotfix. This prevents nested-ZIP extraction from silently running stale code.
- UX refinement: promoted **PID temperature band** and **step-to-slope transition temperature** to Basic controls in Phases 01 and 03. These are operator-facing process-control choices; gains, estimator windows, filters, anti-windup, timeouts and signal-quality thresholds remain in Expert mode.
- Hotfix: restored the Phase 01 robust temperature-slope window constants (`TEMP_SLOPE_WINDOW_S=45 s`, `TEMP_SLOPE_MIN_SPAN_S=20 s`) required by readiness qualification.
- Hotfix: corrected the Qt feedback-controller button construction so the PySide6 dashboard can build without a `TypeError` at startup.
- Added regression tests covering both startup failures reported during the first real Phase 01 run.
- Fixed Phase 01 and Phase 03 startup ordering so QMB signal guards are created only after the QMB device set exists.
- Reworked `START_NPG_CHAMBER.bat` to use the available `python` command, avoid the empty Batch-variable execution bug, reuse a healthy `.venv`, repair moved editable links in place, and install dependencies only when the environment is new or genuinely incomplete.
- Simplified **Change automatization parameters**: routine values stay visible and detailed controller/filter/qualification settings are collapsed under **Expert mode**. Parameter groups are rendered once, so Cascade compound control is no longer split into duplicate sections.
- Removed the historical trusted calibration-ratio reference and its tolerance. Phase 01 calibration quality now relies on exact target crossing and synchronized QMB linearity rather than comparison with an old run.
- Removed historical sample identifiers from current source/user-facing text.
- Removed startup “What changed”, profile/settings dumps, and configuration dumps from phase CMD output; the terminal is reserved for operational state, hardware telemetry, warnings, actions and errors.
- Phase 04 now finishes automatically after the configured 10-minute hold at the final 0 °C setpoint, saves its final files, and closes instead of waiting through hours of passive cooldown data. A final Keysight-output OFF check remains in normal finalization.
- Cleaned distribution-only clutter: removed old migration/runtime backups, historical run data, build cache/wheel artifacts and standalone change/validation Markdown reports. Release changes are consolidated here.

## 0.9.32

- Simplified and verified the zero-current Keysight startup sequence used by Phases 01 and 03 while preserving OCP/OVP and fixed safety limits.

## 0.9.30

- Added live switching between Temperature PID, Rate PID and Compound cascade control in the Phase 01/03 Qt dashboards.
- Improved right-side control-panel organization and serialized mode handovers.

## 0.9.29

- Added inner-loop qualification and delayed-response handling before repeated same-direction cascade actions.

## 0.9.28

- Added robust shutter-readiness qualification, QMB plausibility filtering/auditing, exact-crossing Phase 01 calibration and expanded control summaries.

## 0.9.27

- Added conservative delayed-response timing, robust thickness-slope rate estimation and improved settling/readiness behavior for compound control.

## 0.9.26

- Introduced the true cascade compound controller, professional temperature PID behavior, robust rate estimation and structured decision logging.

## 0.9.25

- Fixed shared live-action state, improved Qt telemetry fault isolation and removed duplicate live Matplotlib startup work.

## 0.9.24

- Separated telemetry acquisition, GUI painting and operator command execution for a more responsive Qt runtime.

## 0.9.23

- Migrated the Phase 01/03 live interface to PySide6 + PyQtGraph while retaining Matplotlib for saved final/snapshot plots.

## Historical release record (v16 and earlier)

### v16 maintenance correction — Phase 02 runtime and skip-Degas automation

- The Windows launcher now verifies the exact pywebview WinForms/pythonnet backend used by Phase 02, confirms that the editable package points to the current project folder, and automatically rebuilds only the local `.venv` when it is copied, incomplete or incompatible. Rebuilds require at least 1 GB free, prefer Python 3.12 when available and install without retaining the pip cache.
- Phase 02 now waits for positive confirmation that the pywebview Windows backend initialized before entering PREFLIGHT. A child-dashboard crash is reported immediately instead of leaving the controller running without usable controls.
- Corrected **Start without initial Degas** without reducing automation. During cycle 1 continuation runs, COSCON `Off` and `Standby` are both accepted as inactive safe states while argon pressure is conditioned. The software then validates the target and issues the normal automatic `SwitchToOperate` command directly from the verified safe state. Normal runs and later cycles continue to require the natural `Standby` state.
- No local COSCON action or new operator button is required. Pressure, interlock, energy, emission, transition and safe-stop checks remain active.
- Kept the internal package version at `0.9.22` as a maintenance correction to project archive v16.

## 0.9.22 - Project archive v16: editable Phase 01/03 safety limits

- Added **Watchdog maximum temperature** to **Change automatization parameters** for Phases 01 and 03. It is an absolute independent CK-1 hard-stop threshold, defaults to `255.0 °C`, and remains separate from the editable `250.0 °C` rate-control temperature ceiling.
- Replaced the previous target-plus-10 °C watchdog hard-limit calculation with the explicit `TEMP_WATCHDOG_MAX_TEMP_C` value. At or above the selected maximum, the established watchdog forces Keysight current to `0 A`, switches output `OFF` and enters `SAFETY_STOP`.
- Added **Maximum automatic current cap** to the Phase 01/03 parameter editor and saved automation modes. The packaged default is `0.660 A` everywhere it controls automatic current.
- Reduced the fixed Phase 01/03 software hard-current stop from `0.685 A` to `0.680 A` in both active runtime scripts and their matching recovery copies. At or above the threshold, the safety path captures the state, commands `0 A`, switches output `OFF` and enters `SAFETY_STOP`.
- Limited the editable automatic-current cap, starting current and base working current to a maximum of `0.675 A`, preserving at least `0.005 A` below the fixed software hard stop.
- Kept the Phase 01/03 Keysight hardware OCP margin at `0.005 A`, so the instrument latch is configured at `0.685 A`. The separate Phase 04 ramp-down OCP setting is unchanged.
- Added validation requiring the watchdog maximum to remain above the rate-control temperature ceiling and the base working current to remain within the selected automatic cap. Updated run summaries, effective parameter records, recovery copies, source manifest, documentation and regression tests.
- Kept the internal package version at `0.9.22`; this safety adjustment remains part of project archive v16 rather than creating a new release.

## 0.9.21 - Project archive v15: CK-1 rate and compound feedback control

- Added a run-selectable **Evaporation feedback mode** to Phases 01 and 03 with `temperature`, `rate` and `compound` choices. The established temperature PID remains the packaged default so existing validated runs do not change silently.
- Added a reusable, hardware-independent CK-1 rate PID implementation with a spike-resistant trimmed rate average, proportional/integral/optional derivative terms, asymmetric current-step limits, current-bound anti-windup and compound temperature-ceiling supervision.
- In rate/compound modes, the normal Steps/Slope warm-up remains active until CK-1 temperature and filtered QMB rate are sufficient for a bumpless handover. Rate PID then remains active through shutter waiting and the complete calibration/evaporation interval.
- Added stable-rate qualification before shutter opening: rate/compound modes require consecutive new QMB readings inside the existing displayed rate band, preventing a single transient from starting deposition.
- Added a dedicated rate-feedback safety timeout. Once rate control is active, a missing or stale CK-1 rate signal causes a graph/summary capture, `SAFETY_STOP`, Keysight current `0 A` and output `OFF` rather than open-loop heating.
- Added an editable rate-control temperature ceiling, rate-PID activation threshold, filter, gains, dead band, update period, asymmetric step limits, integral limit, stable-read count and compound guard band to **Change automatization parameters** and saved full-chamber modes.
- Preserved the independent CK-1 temperature watchdog. In rate/compound modes its soft/hard margins are referenced to the rate-control temperature ceiling; compound mode additionally tapers positive rate-PID corrections before that ceiling is reached.
- Changed the Phase 03 shutter-open QMB reset so only thickness histories restart at zero. CK-1/Sample rate histories remain continuous, avoiding loss of the feedback signal during the control handover.
- Added rate-control state to the Phase 01/03 dashboards, run parameter records and phase summaries, plus regression tests for filtering, directionality, limits, anti-windup, ceiling supervision, mode selection and deposition-phase persistence.
- Updated the internal package version to `0.9.21`, refreshed the matching Phase 01/03 recovery backups and regenerated the source manifest.
- Added `RATE_PID_VALIDATION.md` with supervised first-run, safety-path and gain-tuning checks; the loop is software-tested but is not represented as physically tuned.
- Updated the Windows launcher so an existing local runtime starts the current source tree directly instead of rebuilding the editable package on every launch, avoiding redundant pip cache/disk-space failures while preserving first-time installation behavior.

## 0.9.20 - Project archive v14: PID safe-zero finalization

- Changed the Phase 02 **Abort / Safe Stop** oven PID reset default from `20.0 °C` to `0.0 °C` in both the runtime controller and **Change automatization parameters**. The verified COSCON safe-state and remaining Phase 02 shutdown protections are unchanged.
- Simplified normal Phase 04 completion. After the second annealing hold, the script now writes the configured cooldown target (`0.0 °C` by default), holds that PID setpoint for `10 minutes`, and finishes while leaving the oven PID SV at the same `0.0 °C` value.
- Removed the obsolete Phase 04 command and GUI state that changed the PID from `0 °C` to `30 °C` immediately before completion. The unused **Final PID target** field was removed from the launcher parameter editor.
- Updated Phase 04 **Abort / Safe Stop** to use the same cooldown target (`0.0 °C` by default) before switching the Keysight output off, so normal and abort paths no longer contain a hidden 30 °C command.
- Moved the Phase 04 **Phase sequence** heading and text slightly lower and shortened the sequence description so it remains legible without colliding with the current-status block or **Last action**.
- Added migration compatibility for saved automation modes created before 0.9.20: an old `FINAL_VENT_TARGET_C` entry is discarded automatically instead of preventing the mode from loading.
- Updated the internal package version to `0.9.20`, the v14 documentation, Phase 02/04 explanation PDFs, regression tests, current recovery backups and source manifest.

## 0.9.19 - Phase 02 continuation runs

- Added **Start without initial Degas** to **Change automatization parameters → Phase 02 → Workflow**. It is disabled by default and applies only to the current launcher session.
- When enabled, Phase 02 skips the automatic COSCON Degas before the first configured cycle. This supports continuation runs after an earlier partial Phase 02 execution, for example running one remaining sputter-anneal cycle without repeating Degas.
- Added a conditional preflight confirmation explaining that the option is only for the same chamber preparation after the operator has verified that another Degas is not required. Cancelling that confirmation stops the run before COSCON activation.
- Deliberately excluded this continuation-only checkbox from reusable saved automation modes, so a tutor-approved recipe cannot silently skip Degas in a later chamber preparation.
- Updated the Phase 02 workflow display and safety reminders so Degas is visibly marked as skipped by the launcher setting. The selection is printed in the terminal and saved in the effective run-parameter JSON for traceability.
- Kept Degas enabled for normal runs, kept all pressure/interlock/energy/emission checks unchanged, and retained the existing immediate safe-stop paths.
- Changed the internal package version to `0.9.19` and refreshed the matching Phase 02 recovery backup, source manifest, documentation and regression tests.

### Phase 01/03 temperature-view correction and event-loop responsiveness (same 0.9.19 version)

- Restored the Phase 01 and Phase 03 temperature-view selector/title area slightly lower, keeping the dynamic graph title directly associated with its plot without covering neighbouring graphs.
- Made each selected temperature graph use exactly the same accent as its title and selector: Oven PID red, Pyrometer blue and Sample estimate yellow/gold. Saved comparison plots use the same mapping.
- Moved graph-only PNG snapshot rendering out of the Matplotlib mouse/event loop into a dedicated background saver thread. Snapshot requests no longer freeze button clicks while a 3x3 figure is generated and written.
- Reduced the live display workload to 400 plotted points per series and a 0.50 s full-plot refresh, while preserving complete raw data logging, final saves, experimental logic and all safety behavior.
- Refreshed the matching Phase 01/03 recovery scripts, source manifest and regression tests without changing the internal package version from `0.9.19`.

## 0.9.18 - COSCON confirmation, diagnostics and responsiveness

### COSCON emission confirmation, extra diagnostics and GUI responsiveness

- Changed the Phase 02 emission safety check so the first out-of-tolerance measurement raises an immediate warning and is written to the CSV with its exact timestamp. The measurement is repeated after an editable delay, the counter resets after any valid reading, and safe abort occurs only after the configured number of consecutive anomalous measurements.
- Added Phase 02 launcher fields for COSCON energy tolerance, emission tolerance, consecutive bad emission readings before abort, emission recheck delay and stable-output readings. These remain run-only settings and are recorded with the effective automation parameters.
- Added display/logging-only COSCON diagnostic readbacks for **Energy current**, **Anode voltage** and **Repeller voltage** beside the existing filament value. Diagnostic-command failure does not disable pressure, mode, interlock, energy or emission supervision and cannot itself abort the phase.
- Improved Phase 01 and Phase 03 responsiveness by replacing repeated full-history GUI copies with bounded full-run plot sampling, reducing unnecessary autoscaling/redraw pressure, and moving complete text-file rewrites to a background saver thread. Complete data files and final saves are preserved; PID, Keysight, QMB, watchdog, shutter and phase-transition logic are unchanged.
- Refreshed the matching recovery scripts in `original_scripts_backup/`, updated the source manifest and added regression tests for the new safety, diagnostics and performance paths.

### Parameter-editor usability and Phase 01/03 workflow separation

- Made **Change automatization parameters** a normal resizable/maximizable window that opens at a reduced `1100 x 800` size. The standard Maximize/Restore title-bar control remains available when the operator wants a full-screen workspace.
- Added mouse-wheel scrolling to every long Phase 01–04 and Pyrometer parameter tab, including Windows/macOS wheel events and Linux Button-4/Button-5 events. The vertical scrollbars remain available.
- Removed the separate **Restore validated Au/mica mode** button from the Pyrometer tab. The protected **Au/mica — validated** entry remains available in **Saved material modes**, and the generic reset actions continue to restore packaged defaults when intentionally requested.
- Expanded the Phase 01 and Phase 03 right panels to occupy almost the complete figure height and more of the right-side width. Increased the spacing around **Manual current override**, **Operator controls**, their buttons and the status area so headings and controls do not cover one another.
- Lifted the Phase 01 and Phase 03 **OVEN PID / PYROMETER / SAMPLE EST.** selector buttons above the dynamic temperature-graph title so the title remains fully visible.
- Added a dedicated **Finish phase** button to Phase 03. It is accepted only at the established `WAIT_SHUTTER_CLOSE` stage, after the DP-DBBA target has been reached; it records the shutter-closed confirmation and preserves the normal Phase 03 handoff that leaves the Keysight ON at base current for Phase 04. **Abort / safe stop** remains the separate controlled ramp-down/OFF path.
- Updated the Phase 03 **Abort / safe stop** action to command and verify an oven PID target of `0.0 °C` before continuing the independent controlled Keysight ramp-down and output-OFF sequence. Failure to confirm the PID write is reported prominently but does not prevent the electrical safe stop.
- Reasserted the phase-specific experimental workflows in code and automated checks: Phase 01 contains no 200 °C oven PID write, while Phase 03 alone retains `OVEN_TARGET_TEMPERATURE_C` and the startup setpoint command. The shared Phase 01/03 styling remains presentation-only.
- Refreshed `original_scripts_backup/` with byte-for-byte copies of the four current authoritative phase scripts, as requested, and updated the source manifest. The folder remains excluded from launcher execution.
- Kept the internal project version at `0.9.17` and retained the existing serial-port handoff protections.

### Full-chamber automation modes and Phase 01/03 layout refinement

- Added persistent **Saved automation modes** for complete tutor-approved chamber recipes. Each mode stores every editable startup parameter for Phases 01–04 together with the selected pyrometer profile, while run names, the Phase 01 thickness ratio, COM settings and hard safety limits remain outside the mode.
- Added a dedicated first tab in **Change automatization parameters** with **Load mode**, **Save current as mode** and **Delete** actions. Loading a mode fills all parameter tabs but leaves every field editable for run-specific changes; the saved file and packaged source defaults are never rewritten by those edits.
- Added protected **Packaged defaults** and persistent storage under `Data Samples/Configuration/automation_modes.json`. The selected full-chamber mode name is passed to phase processes and recorded in each effective automation-parameter file for traceability.
- Widened the Phase 01 and Phase 03 right-side control panel and increased its usable height so headings such as **Operator controls** remain fully visible. No control, editable value or status block was removed.
- Moved the **OVEN PID / PYROMETER / SAMPLE EST.** selector into a dedicated slim header immediately above the corresponding temperature graph. The graph title remains above the selector, and the larger row spacing prevents collisions with neighbouring plots.
- Kept the project version at `0.9.17` and preserved all experimental, PID, Keysight, pyrometer, watchdog, shutter and safety behavior.

### Phase 01/03 visual harmonization and cleanup

- Restyled the Phase 01 and Phase 03 live interfaces to match the cleaner visual language used by Phases 02 and 04: pale background, white measurement plots, restrained grids, coloured bold graph headings, light borders, rounded phase badge and pastel control cards.
- Increased the vertical separation between the three graph rows and reserved an independent strip for the temperature selector. Dynamic temperature titles no longer share space with neighbouring plots, so titles and selector buttons cannot cover the graphs above or below.
- Standardized equivalent Phase 01 and Phase 03 panel wording: **Editable heating targets**, **Ramp-up settings**, **Manual current override**, **Operator controls**, **Process status** and **Last action**. Equivalent buttons now use the same labels while every editable field and operator action remains available.
- Kept all experimental values, QMB/PID/Keysight/pyrometer logic, watchdogs, shutter actions, phase transitions, abort paths and saved data unchanged; this maintenance update is presentation-only.
- Removed the separate `CHECK_PYROMETER_EMISSIVITY.bat` and its one-purpose Python wrapper. Emissivity selection and verified hardware readback remain integrated in the normal launcher/phase startup path, avoiding a redundant user-facing tool.
- Added one shared Phase 01/03 styling helper and removed generated cache files from the deliverable. `original_scripts_backup/` remains unchanged.

### 0.9.17 - Functional changes

- Removed **ICN2** from the Phase 02 window title.
- Simplified the permanent Phase 02 telemetry area. COSCON mode and the hardware safety interlock remain active in the automation, CSV log and abort logic, but they are now shown only inside **Auxiliary diagnostics**. The main header and process card show one clear **Waiting / Ready / Check** system result instead of separate technical cards.
- Added persistent user-defined pyrometer material modes. In the launcher **Pyrometer** tab, operators can create, name, save, reload, replace and delete custom modes while the validated **Au/mica — validated** mode remains read-only. Saved modes are stored in `Data Samples/Configuration/pyrometer_profiles.json`.
- Changed the below-cutoff behavior: the estimated sample temperature is now calculated and displayed at every finite pyrometer reading. Values below the selected calibration minimum remain in plots and CSV files and are explicitly marked **WARNING: extrapolated below calibrated range** instead of being replaced by `NaN`.
- Fixed the Phase 01 and Phase 03 **PYROMETER / SAMPLE EST.** views showing `unavailable; retrying`. The previous startup path depended on an emissivity query that this chamber IPE 140 did not answer. Emissivity readback now uses the proven 11-digit `pa` response, and an emissivity setup warning no longer prevents raw temperature monitoring.
- Corrected the emissivity write path to use the standard UPP four-digit tenths-of-a-percent field (`10% -> em0100`, `11% -> em0110`) and verify the result independently through the `pa` parameter reply. The launcher exposes whole percentages because this instrument's LCD and `pa` response report whole-percent emissivity.
- Added the same monitoring-only pyrometer integration and **OVEN PID / PYROMETER / SAMPLE EST.** selector to Phase 04. Phase 04 now saves the selected profile and includes oven, raw pyrometer and estimated sample temperatures in telemetry and comparison plots.
- Kept pyrometer values completely separate from oven PID control, Keysight control, annealing transitions and safety decisions in Phases 01, 03 and 04.
- Expanded automated coverage for persistent material modes, the `pa`-based readback, four-digit UPP emissivity writes, below-range warnings, Phase 04 integration and the revised Phase 02 information hierarchy.

## 0.9.16 - Shared pyrometer profiles and task-focused Phase 02 UX

- Added validated IMPAC IPE 140 integration for Phases 01 and 03 using the laboratory-confirmed serial settings: `COM10`, 38400 baud, 8 data bits, even parity, 1 stop bit and address `00`.
- Added a shared **Pyrometer** tab to **Change automatization parameters**. The launcher now configures the material/calibration profile once and passes it to both pyrometer-enabled phases without rewriting source files.
- Added the validated Au/mica profile: emissivity 10.0%, `T_sample = 1.69959 × T_pyro + 28.20193 °C`, valid from `T_pyro >= 90 °C`. Below that cutoff, the raw temperature is retained while the sample estimate is logged as `NaN` and drawn as a gap.
- Added a **Custom material** profile for future substrates. Operators can set emissivity, slope, intercept, minimum calibrated pyrometer temperature and the default live temperature view. The validated Au/mica values remain locked unless **Custom material** is selected.
- Added controlled emissivity setup at phase startup. The script first reads the instrument setting, writes only when the selected value differs, and verifies the readback. At most one automatic write attempt is made per phase; later retries are read-only. The pyrometer remains monitoring-only and never participates in PID, Keysight, shutter, phase-transition or safety decisions.
- Added a compact three-way selector above the existing temperature graph in Phases 01 and 03: **OVEN PID**, **PYROMETER** and **SAMPLE EST.** The selector changes display only; all three series are continuously logged.
- Updated phase snapshots and final plots to include Oven PID, raw pyrometer and calibrated sample temperature together for post-run comparison. Each run also saves the complete pyrometer profile used.
- Added COM10 to the launcher-side verified serial-port handoff so the pyrometer handle is checked and released before and after every phase.
- Redesigned Phase 02 as a task-focused two-column operator view. The active instruction and required buttons dominate the left side; critical COSCON, interlock, pressure, energy, emission and oven values stay visible on the right. Full workflow, auxiliary diagnostics and reminders are available in expandable sections instead of permanently occupying the screen.
- Preserved all Phase 02 COSCON commands, target validation, timing, pressure/interlock supervision, PID writes, safe-stop behavior and experimental recipe values.
- Added regression tests for the pyrometer protocol, emissivity verification, filtered calibration cutoff, launcher profile handoff, Phase 01/03 selector integration, COM10 release and the new Phase 02 information hierarchy.

## 0.9.15 - Compact white Phase 02 dashboard and honest Degas timing

- Reorganized the Phase 02 dashboard into a compact three-column desktop layout: **OPERATOR GUIDANCE** and **COSCON AND CHAMBER** are grouped in the left column; **TIME INFORMATION**, **OVEN AND AUXILIARY READBACK**, **SYSTEM STATUS** and **OPERATOR REMINDERS** are grouped in the center; and **CURRENT-CYCLE WORKFLOW** occupies the complete right column for maximum legibility.
- Changed the dashboard to a white, high-readability background and gave every information block a distinct light pastel fill matching its accent colour.
- Enlarged the dashboard text, telemetry values, prompts, workflow labels and buttons. All information-card headings remain uppercase, use a stronger version of their block colour, and include a fine black outline for contrast.
- Refined spacing and card density so the normal 1420 x 920 Phase 02 window still fits without horizontal or vertical scrolling. Smaller windows retain a responsive scrollable layout.
- Added a dedicated **SAFETY INTERLOCK** readback and an on-screen explanation: `OK` means the external hardware safety chain permits operation; `Tripped` means activation must be blocked or stopped.
- Removed the inherited 20-minute Degas guide estimate from the workflow and from the known-time estimate. Phase 02 still ends Degas only when COSCON naturally reports `Standby`.
- Renamed the active Degas countdown to **Degas safety timeout left**. The default 25-minute value is only the maximum allowed wait before aborting, not a prediction of how long Degas should take.
- Replaced the obsolete run-only parameter `expected_degassing_minutes` with `degas_timeout_minutes`, labelled **Degas safety timeout**.
- Added **COSCON energy target** and **COSCON emission target** to the Phase 02 tab of **Change automatization parameters**. They default to 2250 V and 10.0 mA, apply only to the current launcher session/run, are shown in the Phase 02 dashboard and terminal summary, and are saved in `automation_parameters.json`.
- The packaged targets remain 2250 V and 10.0 mA; no COSCON commands, pressure limits, oven recipe values, COM ports or safe-stop behavior were changed.

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
