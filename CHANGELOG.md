# Changelog

## Repository publication correction · 2026-08-18

- Re-published the complete `0.9.36` / `2026.08.11-r15` source tree from `npg_chamber_project_v17(9).zip` after the previous GitHub pull request changed only the package build marker.
- Included the four current phase scripts, the shared professional-control and Qt/PyQtGraph modules, launcher and installation logic, complete developer test suite, and all four explanation PDFs under `npg_chamber/script_explanations/`.
- Removed obsolete duplicate active documentation and runtime backup folders while preserving the repository history and empty `Data Samples` directory structure.
- Excluded machine-specific/runtime artifacts (`.venv`, `*.egg-info`, caches, macOS metadata, and experimental run outputs) from the release commit.
- Updated `CITATION.cff` to release `0.9.36` and corrected the source-distribution manifest pattern for `READ ME.md`.
- No chamber-control behavior was changed relative to the supplied v17(9) release source.

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

## 0.9.22

- Set the default watchdog maximum temperature to 255 °C, automatic current cap to 0.660 A and fixed software hard-current stop to 0.680 A.
