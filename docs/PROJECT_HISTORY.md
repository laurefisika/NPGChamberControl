# Project history

This timeline distinguishes file-backed snapshots from milestones reconstructed from the maintained changelog and project records. It does not claim that unavailable intermediate source trees have been recovered.

## March 2026 — Learning the chamber and preserving the starting point

The first two weeks were used to learn the physical synthesis procedure and understand the existing software. At that point, the chamber relied mainly on one real-time monitoring program and on practical operator knowledge.

Two source-backed starting files are preserved:

- a multi-instrument monitoring script for two QMBs, the XGS600 pressure controller, oven PID, Keysight power supply, and CK-1 Arduino temperature readout;
- a Jupyter notebook implementing the staged NPG annealing recipe through the oven PID.

Both files are under `history/2026-03-initial-prototypes/`. The notebook retains historical execution output, so it belongs in the private archive and must be reviewed before any public export.

## April 2026 — Building the four automated phases

April was the main phase-by-phase development period. The original procedure was divided into four independent programs so that each physical stage could have its own state machine, operator prompts and shutdown behavior.

- Phase 01 evolved through real and shortened trials with live QMB, pressure, PID, Keysight and CK-1 plots. An impossible heating-exit condition caused by inverted CK-1 rate limits was identified and corrected. Heating ramps were slowed to values closer to laboratory practice, the shutter remained a guided manual action, and a controlled `RAMP_DOWN` stage was added before finishing.
- Phase 02 was developed as a three-cycle sputtering/annealing controller. It initially guided the operator through the COSCON web page and kept the argon leak valve manual, with explicit open/close confirmation. Automatic leak-valve control was investigated from the manufacturer documentation but was not treated as validated chamber functionality.
- Phase 03 reused the proven heating/calibration structure while adding the DP-DBBA thickness-ratio calculation, temperature/rate qualification, shutter confirmation, deposition target and saved run evidence. Short trial parameters were used before returning to real experimental values.
- Phase 04 converted the staged annealing notebook into a normal Python program that could run from Command Prompt. It coordinated the 350 °C and 550 °C anneals with the Keysight current ramp-down, operator switch-off prompt, final PID setpoints, logging and plots.

The chamber instructions were updated in parallel with short English descriptions of what each program automated and which actions still required the operator.

## May 2026 — Unification, launcher and operator documentation

The four selected phase scripts were collected into one installable project. The source-backed filenames at that stage were `1. Heat up + Calibration_NEW TRY_v7.3.py`, `2. Sputtering-Annealing_NEW TRY_v4.py`, `3. DP-DBBA Evaporation_v7.py` and `4. NPG Annealings_NEW TRY_v3.py`.

- Release `0.8.5` added the verified four-script package and SHA-256 source manifest.
- Release `0.8.6` added the graphical launcher.
- Releases `0.8.7`–`0.8.9` standardized the user-facing structure in English, removed duplicated documentation and placed the operating guide behind a launcher **READ ME** button.
- Releases `0.9.0`–`0.9.7` centralized the four output locations under `Data Samples/`, added one-click Windows startup, run-name fields, automatic Phase 01-to-Phase 03 ratio handoff, cleaner shutdown, startup fixes and the high-contrast pastel launcher design.

The SOP was also rewritten in English while preserving the original photographic sequence. It was expanded with clearer phase instructions, tables, warning boxes and explanations of both the unified launcher and the individual phase interfaces.

## June 2026 — Full-workflow trials, reliability and technical handover

The unified package was used in complete synthesis runs. These trials exposed practical integration problems that isolated script tests did not reveal, especially `PermissionError(13)` failures when the XGS600 or oven PID serial ports remained unavailable between phases. Restarting the workstation was the initial field workaround and the bug register preserved the observations.

Releases `0.9.8`–`0.9.10` added phase explanation PDFs, refined launcher spacing and run-name handling, and improved phase-window closure. The project documentation grew into a reproducible handover set: operating instructions, script explanations, a bug register, screenshots, internship-report annexes and the final presentation **Automatization of the NPG Synthesis Chamber**.

## July 2026 — Configurability and complete instrument integration

- `0.9.11` added a run-only four-phase automation-parameter editor without rewriting the source files or exposing hard safety limits.
- `0.9.12` implemented verified handoff checks for all six chamber COM ports.
- `0.9.13` introduced direct COSCON IS control over its documented UDP ASCII protocol.
- `0.9.14` corrected the COSCON command framing and added a task-focused operator dashboard.
- `0.9.15` refined the Phase 02 interface and aligned displayed Degas time with measured behavior.
- `0.9.16` integrated the IMPAC IPE 140 pyrometer and shared material/calibration profiles.
- `0.9.17` added persistent full-chamber automation modes, completed the Phase 01/03 visual refinement, separated Phase 03 Finish from Abort, and retained phase-specific workflow behavior.
- `0.9.18` made the long parameter editor easier to use, removed the redundant Au/mica restore control, finalized the Phase 01/03 panel spacing, and made Phase 03 Abort command and verify an oven PID target of 0 °C before the independent Keysight ramp-down/OFF path.

The final codebase compiled successfully and recorded 93 passing no-hardware tests on 21 July. Hardware evidence remained deliberately separate: network and protocol tests passed, supervised COSCON Degas completed and returned to Standby after about seven minutes, while the supervised Operate attempt remained inconclusive and was not reported as a pass.

## Snapshot policy

Only two source states are currently backed by complete files:

1. the March prototype snapshot;
2. the integrated `0.9.18` package.

The intermediate releases remain documented in `CHANGELOG.md`, but they should not be represented as recovered code snapshots unless their original ZIP files or scripts are found. If more historical packages are located, each should be added as a separate archival commit and tagged with its actual internal version.
