# Changelog

All notable software changes for the NPG Chamber Controller are recorded here.
Dates use ISO format. Control and safety changes are called out explicitly so
the handover history remains useful at the chamber.

## 0.9.41 - build 2026.09.04-r20 - final handover cleanup

### Packaging and maintainability

- Renamed the active runtime folder from `legacy_scripts` to `phase_scripts`.
- Renamed all four executable phase files to their maintained names and removed
  the obsolete `--run-legacy` command path.
- Renamed the workflow adapter to `runner.py` and simplified its public names.
- Removed unused compatibility helpers, unused control-mode tuple data and
  unused phase functions/imports.
- Standardized the user-facing document name to `README.md`.
- Regenerated `SOURCE_CODE_MANIFEST.json` for the maintained phase files.

### Documentation and verification

- Rewrote the README as the single software Standard Operating Procedure,
  including installation, safety boundaries, defaults, ports, data layout,
  troubleshooting and handover guidance.
- Rebuilt all four phase explanation PDFs with consistent release metadata,
  operator steps, setpoints, safety behavior and data-output notes.
- Added regression coverage for the Phase 02 CSV schema and migration of
  retired fields in saved automation modes.

### Control alignment

- Aligned the Phase 02 pressure-warning default with the central launcher
  recipe: `3.0e-5 mbar`. The target remains `2.0e-5 mbar` and the emergency
  software limit remains `1.0e-4 mbar`.
- No chamber wiring, COSCON activation sequence, current hard stop, watchdog,
  shutter gate, annealing duration or Phase 03 handoff behavior was changed by
  the naming and documentation cleanup.

### Repository publication preparation

- Replaced the repository's historical working tree with the maintained
  `README.md`-based source layout; obsolete snapshots and retired runtime names
  are no longer present in the current tree.
- Added GitHub contribution guidance, issue and pull-request templates, a
  hardware-independent continuous-integration workflow and citation metadata.
- Added explicit project credits and linked the tutor's GitHub profile without
  assigning software authorship beyond the project's stated credits.

## 0.9.40 - build 2026.08.21-r19 - COSCON recovery and plot references

- Preserved one automatic eight-second retry for the exact pre-sputter
  `HV-Module Energy Overload` condition with `Interlock=Ok`.
- Added one guarded COSCON `Reset` recovery after a repeated exact activation
  overload. Reset requires a closed manual argon valve, de-energized output and
  verified safe-state readings; arbitrary errors remain fatal.
- Paused background COSCON polling across the activation transaction and Reset
  handshake to prevent interleaved UDP commands.
- Added reboot proof, a 60-second reconnect window, three safe post-Reset
  samples and a repeated pressure-conditioning interval before the final attempt.
- Improved Phase 01/03 pressure-axis readability, absolute/relative thickness
  display, open/close shutter markers and saved plot regions.

## 0.9.39 - build 2026.08.21-r18 - stable Phase 01 endpoint

- Changed the packaged Phase 01 sample-QMB calibration target to `3.0 angstrom`.
- Required five continuous seconds of fresh sample-QMB readings at or above the
  target before calibration can finish.
- Froze the CK-1/sample thickness ratio at stable confirmation, before physical
  shutter-close reaction time is included.
- Kept first crossing values as audit diagnostics and preserved synchronized QMB
  linearity checks.

## 0.9.38 - build 2026.08.21-r17 - Windows long-path runtime

- Moved the Windows virtual environment to
  `%LOCALAPPDATA%\NPGChamber\runtime_<build>\.venv` so deep project paths do not
  break PySide6 installation.
- Reused healthy runtimes and rebuilt incomplete dependency environments.
- Added launcher checks for the external short runtime path.

## 0.9.37 - build 2026.08.18-r16 - shutter references and relative traces

- Added exact operator open-shutter timestamps to Phase 01/03 dashboards.
- Added persistent open-shutter references to live and saved graphs.
- Switched the live CK-1 and sample thickness traces to shutter-relative values
  starting at `0 angstrom`, while retaining raw process data for analysis.

## 0.9.36 - build 2026.08.11-r15 - phase-local safe stop

- Phase 01 normal Finish performs the controlled ramp-down to zero and output
  off; Abort prioritizes immediate electrical shutdown.
- Phase 03 Abort commands zero current and output off before its best-effort oven
  PID reset; normal Finish preserves the `0.640 A`, output-on handoff to Phase 04.
- Updated the phase explanation documents and regression coverage.

## 0.9.35 - build 2026.08.11-r14 - launcher safety review

- Removed the unified launcher's force-kill path for active phase processes.
- Launcher Close, the window X and launcher-level Ctrl+C now direct the operator
  to the active phase's own `Abort / Safe Stop` path.
- Removed the unused generic process-termination helper.

## 0.9.34 - build 2026.08.10-r13 - readiness cleanup

- Removed the obsolete Phase 01/03 shutter-readiness diagnostic layer.
- Kept the minimal process gate: CK-1 temperature and averaged rate must reach
  target; Phase 03 also requires its external-oven readiness condition.
- Clarified that cascade slope and trend thresholds are controller-internal,
  not hidden shutter conditions.
- Refreshed the four explanation PDFs and retained all active safety guards.

## 0.9.33 - build 2026.08.10-r11 - readiness and ratio confirmation

- Restored the documented Phase 01/03 shutter transition criteria after a
  readiness regression.
- Added explicit Phase 03 thickness-ratio confirmation with manual fallback.
- Kept external-oven stability as an independent Phase 03 prerequisite.

## Earlier releases

- `0.9.33` / `2026.08.07`: hardened COSCON activation recovery and added expert
  retry parameters.
- `0.9.33` / `2026.08.07`: staged live target editing, axis formatting,
  adaptive graph fitting and clearer operator-panel ordering.
- `0.9.33` / `2026.08.07`: moved source verification into the read-only
  `installation_check` module and improved Windows runtime repair.
- `0.9.32`: simplified and verified zero-current Keysight startup.
- `0.9.30`: added live Temperature PID, Rate PID and Compound control selection.
- `0.9.29`: added inner-loop qualification and delayed-response handling for
  cascade actions.
- `0.9.28`: added QMB plausibility auditing, shutter qualification and structured
  control summaries.
- `0.9.27`: added delayed-response timing and robust thickness-slope estimation.
- `0.9.26`: introduced the compound cascade controller and professional PID.
- `0.9.25`-`0.9.24`: separated live actions, telemetry and GUI painting for a
  more responsive runtime.
- `0.9.23`: migrated the Phase 01/03 live interface to PySide6/PyQtGraph while
  retaining Matplotlib for saved plots.
- `0.9.22`: established the `255 deg C` watchdog, `0.660 A` automatic cap and
  `0.680 A` fixed software hard-current stop.
