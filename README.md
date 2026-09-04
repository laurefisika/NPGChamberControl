# NPG Chamber Controller

Software release for the ICN2 NPG synthesis chamber.

| Item | Value |
| --- | --- |
| Release | `0.9.41` |
| Build | `2026.09.04-r20` |
| Supported Python | `3.10+` |
| Primary entry point | `npg-chamber` |
| Operating system | Windows recommended; source tools also run on Linux/macOS |

This README is the software Standard Operating Procedure. It explains how to
start, supervise and verify the controller. It does not replace the laboratory
SOP for the chamber use.

## Project context

This controller was developed during an internship at the Catalan Institute of
Nanoscience and Nanotechnology (ICN2) in the Atomic Manipulation and Spectroscopy Group for the NPG synthesis chamber. The
software repository contains the maintained source tree, phase explanations,
tests and Windows launcher.

## Credits and acknowledgements

- **Project development and documentation:** Laura Rodríguez Jordán.
- **Original base code:** Roger Simon de Febrer.
- **Scientific supervision and training:** Piotr Krzysztof Ciochon ([Cj111gh](https://github.com/Cj111gh/)).

I gratefully acknowledge the supervision, training and practical
guidance provided throughout the ICN2 internship.

## Publication status

This is the final software handover release prepared for repository review.
The repository remains private while ownership, laboratory approval and the
appropriate publication terms are confirmed. The software is not released for
public reuse by this README alone; see `LICENSE.md` before sharing or adapting
the code.

## 1. Safety boundary

The chamber is experimental hardware. Before every run:

1. Follow the current laboratory chamber SOP and confirm that the chamber,
   cooling, gas supply, leak valve and sputter-gun setup are ready.
2. Confirm that the selected sample name, phase and recipe are correct.
3. Confirm that every physical interlock is closed and that the configured COM
   ports belong to the intended instruments.
4. Stay available throughout the run. The software dashboard is not a
   substitute for physical supervision.

Each phase script owns its instrument-control and safe-stop logic. The unified
launcher starts the phase as a separate process, waits for it to release all
configured serial ports, and never force-terminates an active phase. The GUI
also blocks its Close action while a phase is running. Use the phase's
`Abort / Safe Stop` control and resolve the physical condition before closing
the launcher.

The software limits described here are software safeguards, not a replacement
for instrument hardware limits or the laboratory emergency procedure.

## 2. Start the software

### One-click start on Windows

From the project folder, double-click:

```text
START_NPG_CHAMBER.bat
```

The batch launcher:

- checks that it is running from the project folder;
- creates or repairs a dedicated runtime at
  `%LOCALAPPDATA%\NPGChamber\runtime_2026.09.04-r20\.venv`;
- verifies the installed dependencies and the editable project link;
- runs the read-only source verifier before opening the GUI;
- starts `python -m npg_chamber`.

The runtime is kept outside the project folder because Qt dependencies contain
deeply nested paths. A failed dependency or source check stops before any phase
script is started.

### Manual installation

From a terminal opened in the project root:

```text
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m npg_chamber --list
python -m npg_chamber
```

The last command opens the graphical launcher. Use the text menu when a GUI
backend is unavailable:

```text
python -m npg_chamber --text-menu
```

The packaged runtime dependencies cover serial I/O, the Qt and Matplotlib
dashboards, COSCON's webview-compatible support, pyrometer access and numeric
analysis. Do not install dependencies into a different interpreter and assume
that the launcher will use it.

## 3. Launcher and command line

The graphical launcher presents four phase cards, a run name for each phase,
Phase 01 to Phase 03 ratio handoff, saved automation modes and one explanation
PDF per phase. The green `README` button opens this file.

Useful commands:

```text
npg-chamber --list
npg-chamber --version
npg-chamber --run heat
npg-chamber --run sputter
npg-chamber --run dpdbba
npg-chamber --run anneal
npg-chamber --text-menu
```

`--run` starts one packaged phase directly. The normal command without options
opens the GUI. Direct phase execution is useful for diagnostics and controlled
handoff testing, but it does not remove the requirement for physical
pre-flight checks.

The GUI lets the operator change validated run-only parameters without editing
the Python source. These values are passed to the selected phase through an
environment variable and are not written back to the source files. Reusable
automation modes store approved phase recipes, but the immediate decision to
skip the Phase 02 initial Degas is never persisted in a reusable mode.

## 4. Packaged phase scripts

The four files in `npg_chamber/phase_scripts/` are the authoritative executable
phase implementations. Their workflow keys and explanation documents are:

| Phase | Workflow key | Script | Explanation |
| --- | --- | --- | --- |
| 01. Heat up + Calibration | `heat` | `01_heat_up_calibration.py` | `01_heat_up_calibration_explanation.pdf` |
| 02. Sputtering-Annealing | `sputter` | `02_sputtering_annealing.py` | `02_sputtering_annealing_explanation.pdf` |
| 03. DP-DBBA Evaporation | `dpdbba` | `03_dp_dbba_evaporation.py` | `03_dp_dbba_evaporation_explanation.pdf` |
| 04. NPG Annealings | `anneal` | `04_npg_annealings.py` | `04_npg_annealings_explanation.pdf` |

The small modules in `npg_chamber/workflows/` are entry-point adapters. They
select the corresponding phase script; they do not duplicate experimental
logic.

### Phase 01 - Heat up + Calibration

The packaged defaults are:

- CK-1 temperature guide: `242 deg C`;
- CK-1 deposition-rate guide: `0.40 angstrom/s`;
- sample-QMB calibration target: `2.0 angstrom`;
- calibration endpoint: five continuous seconds of fresh sample-QMB readings;
- normal automatic current cap: `0.660 A`;
- software hard stop: `0.680 A`;
- independent CK-1 watchdog: `255 deg C`;
- initial current: `0.005 A`, followed by the validated ramp recipe;
- default ramp: current steps every `15 s` until `100 deg C`, then slope control;
- temperature PID band: `+/-0.7 deg C`.

The calibration ratio is computed from the CK-1 and sample QMB relative
thicknesses. The shutter gate requires the explicit process conditions checked
by the script, including the target CK-1 temperature and rate and the stable
sample-QMB calibration endpoint. The dashboard marks shutter-open and
shutter-close events and shades the active deposition interval.

`Finish` performs the normal controlled ramp-down and leaves a complete report.
`Abort / Safe Stop` prioritizes the electrical safe state and stops the phase
without waiting for normal completion. Keep the phase window open until the
launcher confirms that the process has exited and released its serial ports.

### Phase 02 - Sputtering-Annealing

The packaged defaults are:

- `3` sputtering-annealing cycles;
- one automatic COSCON Degas before cycle 1;
- `20 min` sputtering per cycle;
- COSCON target: `2250 V` and `10.0 mA` emission;
- stable target confirmation: five valid consecutive readings;
- argon pressure target: `2.0e-5 mbar`;
- pressure warning: `3.0e-5 mbar`;
- software emergency pressure limit: `1.0e-4 mbar`;
- annealing target: `620 deg C` for `10 min`;
- PID reset after each anneal: `0 deg C`.

The argon leak valve and the sputter-gun pre-flight remain manual. The operator
must follow the dashboard prompts. COSCON activation uses an exclusive sequence:
pause background polling, validate the target, wait for the quiet interval,
then switch to Operate and verify mode, interlock, energy, emission and
pressure. Only the exact `HV-Module Energy Overload` activation condition is
eligible for the single default eight-second retry. If it repeats, the guarded
Reset recovery is limited to the documented safe, de-energized state and one
default Reset. Arbitrary COSCON errors are fatal and cause a safe stop.

The pressure warning is operator guidance; the emergency limit is a software
stop. Both must be interpreted together with the physical vacuum system and
laboratory procedure.

### Phase 03 - DP-DBBA Evaporation

Phase 03 requires a confirmed Phase 01 thickness ratio. The launcher first
offers the latest valid Phase 01 ratio and asks for explicit confirmation. If
the ratio is unavailable or rejected, enter it manually before starting.

The packaged defaults are:

- DP-DBBA sample-equivalent target: `623.13 / 94.39 angstrom`;
- external oven startup target: `200 deg C`;
- oven readiness band: `+/-2 deg C` for `60 s`;
- CK-1 guide: `242 deg C` and `0.40 angstrom/s`;
- the same automatic current cap and `0.680 A` software hard stop as Phase 01.

The shutter cannot open until the oven readiness, CK-1 temperature, CK-1 rate
and ratio-dependent process checks pass. Absolute and relative thickness are
recorded in the live dashboard and report.

On normal `Finish`, Phase 03 returns the Keysight current to the Phase 04
handoff value of `0.640 A` and leaves the output enabled for the next phase.
On `Abort / Safe Stop`, it commands zero current and switches the output off,
with the oven PID reset attempted as a best effort.

### Phase 04 - NPG Annealings

The packaged default recipe is:

| Stage | Target | Stability | Hold |
| --- | ---: | ---: | ---: |
| Initial wait | `200 deg C` | recipe-controlled | `5 min` |
| First anneal | `350 deg C` | `+/-2 deg C` for `30 s` | `15 min` |
| Second anneal | `600 deg C` | `+/-2 deg C` for `30 s` | `40 min` |
| Cooldown | `0 deg C` | verified by the PID signal | `10 min` |

The default hold policy pauses timed annealing while the oven is outside its
stability band. The Keysight handoff starts at `0.640 A`, waits for the
configured delay, ramps down in `0.005 A` steps every `15 s`, and switches the
output off at zero current. The external oven PID is physically autonomous;
remain supervised until the cooldown setpoint and process value have been
verified.

## 5. Instruments and default serial ports

The current chamber wiring defaults are centralized in
`npg_chamber/config/ports.py` and are also used by the phase checks:

| Device | Port | Serial settings |
| --- | --- | --- |
| CK-1 QMB | `COM4` | `115200 baud` |
| Sample QMB | `COM16` | `115200 baud` |
| XGS600 pressure gauge | `COM6` | `9600 baud` |
| Oven PID | `COM9` | `9600 baud` |
| Keysight power supply | `COM17` | `9600 baud` |
| CK-1 Arduino | `COM3` | `9600 baud` |
| IMPAC IPE 140 pyrometer | `COM10` | `38400 baud`, `8E1`, address `00` |

Verify the physical wiring before changing a port. A port that is merely
available is not necessarily the correct instrument.

## 6. Data and output files

All run data are written below:

```text
Data Samples/
├─ Heat up + Calibration Data/
├─ Sputtering-Annealing Data/
├─ DP-DBBA Evaporation Data/
└─ NPG Annealing Data/
```

Each phase reserves a new folder using the common pattern:

```text
<phase name> <sample name> data <two-digit counter>
```

For example:

```text
Heat up + Calibration SC67 data 00
Sputtering-Annealing SC67 data 00
DP-DBBA Evaporation SC67 data 00
NPG Annealings SC67 data 00
```

Counters start at `00` and existing folders are never overwritten. Windows
unsafe characters in sample names are replaced before the folder is created.
The allocator creates the directory atomically, so two launches cannot claim
the same number.

Depending on the phase, a run folder can contain CSV telemetry, text event and
parameter logs, JSON snapshots, calibration summaries, plots and final report
figures. Phase 02's `sputter_anneal_log.csv` keeps one stable header/data width
for COSCON energy, emission, filament, anode and repeller telemetry.

The controller also stores reusable automation modes under
`Data Samples/Configuration/`. Existing data folders and saved modes remain
readable after this software cleanup; retired fields are discarded only during
validated mode migration.

## 7. Verification and diagnostics

Run the source verifier from the project root before a handover or after
repairing an installation:

```text
python -m npg_chamber.installation_check --expected-build 2026.09.04-r20
```

The verifier performs text-level checks only. It does not import phase scripts,
open serial ports or initialize hardware.

The developer test suite is hardware-independent and should pass in the project
environment:

```text
python -m compileall -q npg_chamber diagnostic_tools maintenance_tools developer_tests
python -m pytest -q
```

Useful read-only tools are in `diagnostic_tools/`:

- `check_all_common_devices.py` checks the reusable device modules;
- `check_oven_pid_connection.py` checks the oven PID connection path;
- `analyze_control_run.py` summarizes saved control data.

Do not use a diagnostic tool as a substitute for the phase safe-stop procedure.

## 8. Project layout

```text
npg_chamber_project_v20/
├─ npg_chamber/
│  ├─ cli.py                         # CLI and text-menu entry point
│  ├─ gui_launcher.py                # unified graphical launcher
│  ├─ installation_check.py          # read-only source verifier
│  ├─ phase_scripts/                 # four authoritative phase scripts
│  ├─ script_explanations/           # operator explanation PDFs
│  ├─ common/                        # shared control, paths and UI helpers
│  ├─ config/                        # ports, parameters and mode validation
│  ├─ devices/                       # reusable instrument protocols
│  └─ workflows/                     # thin phase entry-point adapters
├─ developer_tests/                  # hardware-independent regression tests
├─ diagnostic_tools/                 # read-only diagnostic scripts
├─ maintenance_tools/                # project maintenance checks
├─ CHANGELOG.md
├─ SOURCE_CODE_MANIFEST.json
├─ START_NPG_CHAMBER.bat
└─ pyproject.toml
```

`SOURCE_CODE_MANIFEST.json` records the SHA-256 and byte size of each packaged
phase script. Update it whenever one of those scripts changes.

## 9. Troubleshooting

### The Windows launcher stops before opening the GUI

Read the first `[FAIL]` line in the terminal. Common causes are an incomplete
runtime, a broken editable link, a missing dependency or a project folder that
was moved after installation. Run the batch launcher again so it can repair the
runtime, then run the source verifier manually.

### A phase reports that a COM port is unavailable

Close other instrument software, confirm the port assignment in Device Manager,
and ensure that a previous phase has finished its serial handoff. Do not start
the next phase until the launcher reports that all chamber ports are reusable.

### The GUI is open but a phase will not start

Check the run name, the ratio confirmation for Phase 03, the pre-flight prompts
and the terminal output. A stale port or a failed source check must be resolved
before retrying.

### The launcher Close button does nothing during a run

This is intentional. Use `Abort / Safe Stop` in the active phase, wait for the
phase process to exit and let the launcher verify the serial handoff.

### The pressure or COSCON dashboard shows a warning

Follow the current phase prompt and inspect the physical chamber and gas/vacuum
system. The dashboard reports software state; it cannot determine whether a
mechanical valve, cable or interlock is physically correct.

### A previous run folder is present

Do not delete or reuse it while investigating a run. Start a new run name or
allow the allocator to choose the next numbered folder. The software is designed
to preserve earlier data rather than overwrite it.

## 10. Handover notes

- Read `CHANGELOG.md` before modifying control logic.
- Keep the four explanation PDFs and this README synchronized with the phase
  defaults and safety behaviour.
- Run the verifier, compile check and full test suite before packaging.
- Regenerate `SOURCE_CODE_MANIFEST.json` after changing a phase script.
- Treat hardware validation as a separate supervised activity; this package's
  automated tests do not connect to the chamber.
- The retired source folder and fallback command are intentionally not part of
  this release. Historical data and saved-mode migration support are retained
  where they protect reproducibility.

## 11. Versioning

The package version is defined in `npg_chamber/__init__.py` and mirrored in
`pyproject.toml`. The Windows launcher uses the build identifier to isolate its
runtime. Keep the package version, build identifier, changelog heading and
source manifest synchronized for every handover.
