# READ ME — NPG Chamber Controller SOP

**Project:** ICN2 NPG synthesis chamber workflows  
**Package name:** `npg-chamber`  
**Current release:** `0.9.18`  

### Parameter-editor usability and safer Phase 03 abort behavior in 0.9.18

The **Change automatization parameters** window opens at a reduced `1100 x 800` size and can be resized or maximized normally. Long Phase 01–04 and Pyrometer tabs support mouse-wheel scrolling as well as their vertical scrollbars. The redundant **Restore validated Au/mica mode** button has been removed; the protected validated mode remains available in **Saved material modes**.

The Phase 01 and Phase 03 right panels use more of the available height and width so **Operator controls** and its buttons remain visible. Their **OVEN PID / PYROMETER / SAMPLE EST.** selectors sit above the dynamic graph titles without covering them.

Phase 03 keeps **Finish phase** and **Abort / safe stop** as separate actions. Normal Finish preserves the established handoff to Phase 04. Abort first commands and verifies an oven PID target of `0.0 °C`, then continues with the independent controlled Keysight ramp-down and output-OFF sequence. A failed PID confirmation is reported but does not block the electrical safe stop.

### Capabilities retained from 0.9.17

The **Saved automation modes** tab inside **Change automatization parameters** stores complete tutor-approved chamber recipes. A mode contains every editable startup value for Phases 01–04 plus the selected pyrometer profile. Examples can be named **NPG at 600 C** or **GNR at 500 C**. Loading a mode fills every parameter tab in one step, after which the operator can still change any field for the current launcher session. Full-chamber modes are stored in:

```text
Data Samples/Configuration/automation_modes.json
```

Run names, the Phase 01 thickness ratio, COM ports, baud rates and hard safety limits are deliberately not stored in these modes.

The shared **Pyrometer** tab inside **Change automatization parameters** now supports persistent material/calibration modes. Operators can create, name, save, reload and delete custom modes; the validated **Au/mica — validated** mode remains protected. Each mode keeps the instrument emissivity, sample-temperature slope/intercept, minimum calibrated raw temperature and default graph view together. Modes are stored in:

```text
Data Samples/Configuration/pyrometer_profiles.json
```

The validated Au/mica mode uses 10% emissivity and:

```text
T_sample = 1.69959 × T_pyro + 28.20193 °C
calibrated for T_pyro >= 90 °C
```

Below the selected minimum, the estimated sample temperature is still calculated, plotted and saved, but the GUI and logs mark it as **extrapolated below calibrated range**. The IPE 140 remains monitoring-only and never changes PID, Keysight, shutter, phase-transition or safety decisions.

The IPE 140 uses `COM10`, 38400 baud, 8E1 and address `00`. Phases 01, 03 and 04 now share the compact **OVEN PID / PYROMETER / SAMPLE EST.** selector. All available temperature series are logged regardless of the selected live view. Emissivity readback uses the parameter query proven on the chamber instrument. A requested change uses the UPP four-digit setting command and is then verified independently from the returned parameter string. An emissivity setup warning no longer disables temperature monitoring.

Phase 02 keeps COSCON mode and the hardware interlock in its automation and safety checks, but they no longer occupy prominent permanent cards. The operator sees one simple **Waiting / Ready / Check** system result; the technical mode and interlock values remain available under **Auxiliary diagnostics**. The title no longer contains **ICN2**.

Phase 01 and Phase 03 now use the same visual system as the newer dashboards: a rounded live-phase badge, coloured graph headings, wider spacing between graph rows, a dedicated temperature-selector strip and pastel control cards. Their equivalent controls use the same wording and order, while all original editable fields, shutter actions, manual-current controls, abort/finish actions and experimental logic remain available.

**Purpose:** provide one clean, installable launcher for the four final chamber scripts while preserving the internal logic of those scripts.

---

## 1. Scope and safety note

This document is the single Standard Operating Procedure for the packaged project. It explains:

- what the package contains;
- how to install it on a Windows PC;
- how to start the graphical launcher;
- what each of the four phases does;
- what the GUI buttons do;
- useful command-line commands;
- what files/folders are created;
- how to fall back to the original scripts;
- what to check before using the real chamber hardware.

The package only organizes and launches the scripts. It does **not** replace operator supervision. Always check the chamber, Keysight, PID, pressure, shutters, leak valve, sputter electronics, and oven state before and after every phase.

---

## 2. What this package does

Before packaging, the workflow consisted of four separate Python scripts. This project wraps them into one installable Python package called `npg_chamber`.

The main command is:

```bat
npg-chamber
```

When launched without extra options, it opens a graphical window with buttons for:

1. Heat up + Calibration
2. Sputtering-Annealing
3. DP-DBBA Evaporation
4. NPG Annealings

Each button launches the corresponding final script as a separate Python process. The launcher provides GUI startup fields for the run name of each phase. For DP-DBBA, the launcher automatically reuses the thickness ratio saved by Phase 01 during the same launcher session; it only asks for the ratio manually if the launcher was restarted or Phase 01 was not run first. The scripts still keep their own plots, GUI windows, prompts, serial communication, and safety logic. The CMD window remains open for logs and any rare extra prompts that may still be required during a run.

The package also provides direct command-line launch options, diagnostics, and direct fallback access to the authoritative packaged scripts.

---

## 3. What has not been changed

The four authoritative runtime scripts are preserved inside:

```text
npg_chamber/legacy_scripts/
```

A separate recovery/reference snapshot of the four current scripts is retained inside:

```text
original_scripts_backup/
```

The launcher never imports or executes the backup folder automatically. In this package, each backup is a byte-for-byte copy of its current authoritative runtime script, so a maintainer can restore a working file if the active copy is accidentally damaged.

Except for changes explicitly recorded in `CHANGELOG.md`, the packaging layer does **not** change:

- experimental variables;
- current limits;
- setpoints;
- PID constants;
- timings;
- pressure thresholds;
- QMB logic;
- Keysight logic;
- Arduino reading logic;
- shutter logic;
- abort logic;
- ramp-down logic;
- experimental file contents or naming inside each run folder.

The intentional packaging-level changes are limited to user convenience: every run saves under `Data Samples/<phase data folder>/`, the graphical launcher passes startup run names, the Phase-1 thickness ratio can be handed to DP-DBBA automatically, and validated run-only automation recipe values can be passed to the selected child process. The launcher never rewrites the script files; hardware communication and control logic remain inside the four phase scripts.

---

## 4. Final scripts included

| Phase | Launcher key | Packaged script |
|---|---:|---|
| 1. Heat up + Calibration | `heat` | `01_heat_up_calibration_legacy.py` |
| 2. Sputtering-Annealing | `sputter` | `02_sputtering_annealing_legacy.py` |
| 3. DP-DBBA Evaporation | `dpdbba` | `03_dp_dbba_evaporation_legacy.py` |
| 4. NPG Annealings | `anneal` | `04_npg_annealings_legacy.py` |

The exact hashes and file sizes of both the active runtime scripts and the preserved backup copies are recorded in:

```text
SOURCE_CODE_MANIFEST.json
```

Use that file to verify which exact versions were packaged and whether either copy has been modified.

---

## 5. Folder structure

```text
npg_chamber_project/
├─ READ ME.md                  # this SOP and full user guide
├─ START_NPG_CHAMBER.bat       # double-click Windows launcher
├─ CHANGELOG.md                # release history and important changes
├─ LICENSE.md                  # project license placeholder
├─ MANIFEST.in                 # packaging include rules
├─ SOURCE_CODE_MANIFEST.json   # SHA256 hashes of packaged workflow scripts
├─ pyproject.toml              # Python packaging configuration
│
├─ Data Samples/               # all generated run data are saved here
│  ├─ Heat up + Calibration Data/
│  ├─ Sputtering-Annealing Data/
│  ├─ DP-DBBA Evaporation Data/
│  ├─ NPG Annealing Data/
│  └─ Configuration/            # persistent chamber modes and pyrometer material modes
│
├─ npg_chamber/                # installable Python package
│  ├─ cli.py                   # command-line entry point: npg-chamber
│  ├─ gui_launcher.py          # graphical launcher with phase buttons
│  ├─ legacy_scripts/          # final packaged scripts executed by the launcher
│  ├─ workflows/               # small wrappers that run each packaged script
│  ├─ devices/                 # reusable device helper modules
│  ├─ common/                  # shared helper utilities
│  └─ config/                  # shared port/default configuration helpers
│
├─ original_scripts_backup/    # current phase-script snapshot; recovery/reference only
├─ diagnostic_tools/           # optional manual hardware diagnostic scripts
├─ developer_tests/            # safe tests that do not require chamber hardware
└─ maintenance_tools/          # general package check script
```

### What is required for normal use?

Required:

```text
npg_chamber/
pyproject.toml
READ ME.md
SOURCE_CODE_MANIFEST.json
```

Strongly recommended:

```text
CHANGELOG.md
original_scripts_backup/
```

The backup is not needed to run the chamber, but it should be kept with the laboratory project for recovery and source comparison.

Optional but useful:

```text
diagnostic_tools/
developer_tests/
maintenance_tools/
```

The optional folders are kept because they are useful for troubleshooting, validation, and future maintenance.

The release contains one authoritative runtime copy of each phase script under `npg_chamber/legacy_scripts/` and one current recovery/reference snapshot under `original_scripts_backup/`. All four backup files are byte-for-byte identical to their matching runtime files at release time. The backup is excluded from normal launcher and workflow execution.

Generated files such as `__pycache__/`, `.pytest_cache/`, `build/`, `dist/`, and `*.egg-info/` are not part of the clean release ZIP. They may appear locally after running tests or building a wheel, and they can be deleted safely.

The four experimental scripts remain independent because each phase runs as its own process and owns different hardware and safety behavior. Cleanup removes only proven dead helper code, unused imports from active runtime files, and generated artifacts. The intentionally preserved script backup is an explicit exception; similar-looking phase logic is not merged when doing so could change experimental behavior.

---

## 6. Installing on a Windows PC

Open CMD or PowerShell inside the project folder. The folder must contain `pyproject.toml`.

Create a virtual environment:

```bat
python -m venv .venv
```

Activate it:

```bat
.venv\Scripts\activate
```

Upgrade pip:

```bat
python -m pip install --upgrade pip
```

Install the project in editable mode:

```bat
python -m pip install -e .
```

The final dot `.` is important. It means “install the project in the current folder”.

Check the installation:

```bat
npg-chamber --version
npg-chamber --list
```

Expected version:

```text
0.9.18
```

---


### One-click start on Windows

For daily use, you do **not** need to type `.venv\Scripts\activate` manually. Use the file in the project root:

```text
START_NPG_CHAMBER.bat
```

Double-clicking this file will:

1. open the project folder;
2. create `.venv` if it does not exist yet;
3. install the package the first time;
4. refresh the editable installation after project updates;
5. open the graphical launcher.

This is the recommended way to start the unified code on the chamber PC.

---

## 7. Starting the graphical launcher

If you use the one-click launcher, double-click `START_NPG_CHAMBER.bat`.

If you prefer CMD manually, with the virtual environment active, run:

```bat
npg-chamber
```

A GUI window opens with:

- a large centered **NPG Chamber Controller** title;
- four high-contrast pastel phase cards, each with a very light matching card background;
- a run-name field for each phase;
- an automatic DP-DBBA ratio status line;
- a lavender **Change automatization parameters** button with a Saved automation modes tab, Phase 01-04 tabs and a shared Pyrometer tab;
- a green **READ ME** button;
- a red **Close** button;
- a status area.

The scripts themselves still run in the CMD window so logs remain visible. For normal startup, enter the phase run names in the GUI. The DP-DBBA thickness ratio is reused automatically after Phase 01 if the launcher stayed open. Run-only automation parameters can be prepared before a phase starts; they are passed to that child process without rewriting the source files. If a script later needs a rare extra command or diagnostic input, type that in the CMD window.

If you press the red **Close** button while a phase is running, the launcher stops the running phase process and closes the GUI. The `START_NPG_CHAMBER.bat` window then exits automatically on normal close; you do not need to press any key.

---

## 8. GUI buttons

### Phase buttons

| Button | What it launches | Color |
|---|---|---|
| `Start 01 Heat up + Calibration` | Heat up + Calibration | medium pastel red button on very light red card |
| `Start 02 Sputtering-Annealing` | Sputtering-Annealing | medium pastel blue button on very light blue card |
| `Start 03 DP-DBBA Evaporation` | DP-DBBA Evaporation | medium pastel yellow button on very light yellow card |
| `Start 04 NPG Annealings` | NPG Annealings | medium pastel green button on very light green card |

When a phase is running, the launcher disables the phase buttons and the automation-parameter editor to avoid changing startup values after a child process has begun. Each card shows the phase number and phase name on one line, for example `01 Heat up + Calibration`. The number is shown in the phase color and the phase name/card text is shown in black for readability.

### Startup input fields

Each phase card has a **Run name** field. The launcher sends that value to the selected script before the script starts. This avoids typing the initial run name in CMD when using the GUI.

The DP-DBBA phase no longer requires a visible ratio field during the normal sequence. After Phase 01 finishes, the launcher reads the saved `thickness_ratio` from the Heat up + Calibration summary file and stores it for the current GUI session. When Phase 03 starts, the launcher passes that ratio automatically to the DP-DBBA script. If the launcher was restarted, or if Phase 01 was not run first, then the launcher opens a small pop-up asking for the positive thickness ratio manually.

The CMD window still remains open for logs and for rare extra script prompts, such as diagnostic commands or fallback hardware values if a device cannot be read automatically.

### Change automatization parameters button

The lavender **Change automatization parameters** button opens a maximizable editor with a first **Saved automation modes** tab, one tab for each phase, and one shared **Pyrometer** tab. On Windows it opens maximized by default and retains the normal Maximize/Restore title-bar control. In every long phase or Pyrometer tab, the mouse wheel scrolls the parameter page directly; the scrollbar remains available as an alternative. The phase tabs are intended for values that define the automation recipe, such as:

- temperatures, deposition/rate targets and cycle counts;
- sputtering, annealing, hold and wait durations;
- Keysight ramp-up/ramp-down step sizes and periods;
- the CK-1 ramp mode and temperature-slope targets;
- CK-1 PID gains, PID period, band and correction limit;
- the Phase 03 DP-DBBA sample-equivalent thickness target.

The editor deliberately does **not** expose COM ports, baud rates, device addresses, plotting/logging settings or hard Keysight current/voltage safety limits.

The **Saved automation modes** tab is intended for tutor-approved, repeatable recipes. Use **Load mode** to fill all Phase 01–04 and Pyrometer tabs, **Save current as mode** to create or replace a reusable full-chamber recipe, and **Delete** to remove a custom mode. **Packaged defaults** is protected and cannot be overwritten or deleted. Loading a mode never locks the fields: the operator can still make run-specific changes before pressing **Apply for this launcher run**. Reusable chamber modes are stored in `Data Samples/Configuration/automation_modes.json`.

The **Pyrometer** tab is shared by Phases 01, 03 and 04. It provides:

- enable/disable monitoring for the current launcher session;
- a protected **Au/mica — validated** mode;
- **New mode**, **Save mode** and **Delete** controls for persistent custom material modes;
- whole-percent instrument emissivity from 10% to 100%;
- sample-temperature calibration slope and intercept;
- the minimum raw pyrometer temperature above which the mode is experimentally calibrated;
- the default live graph view;
- optional write-and-verify of emissivity at phase startup.

Communication settings (`COM10`, 38400 baud, 8E1, address `00`) remain fixed and are not ordinary experimental parameters. Changing emissivity without a matching material calibration can make the estimated sample temperature inaccurate, so each saved mode keeps emissivity and calibration values together. The selected mode is saved with every run, while reusable custom modes are stored in `Data Samples/Configuration/pyrometer_profiles.json`.

Below a mode's minimum calibrated raw temperature, the software still calculates and displays the sample estimate. It adds an extrapolation warning instead of hiding the curve or writing `NaN`.

After editing, press **Apply for this launcher run**. The launcher validates the values and keeps them only in memory for the current launcher session. When a phase starts, only that phase's changed values are serialized into a JSON environment variable and read by the child script. The source `.py` files are not edited. Closing and reopening the launcher starts from the packaged default; a saved chamber mode can then be loaded again in one step.

Use **Reset current phase** to restore the open tab or **Reset all** to restore all four phase recipes before applying. The editor is disabled while a phase is running.

Each phase prints the active overrides in CMD and saves an `automation_parameters.json` file in the run output folder. That file records both the explicit overrides and the complete effective startup parameter set passed to the phase. Live edits made later inside a phase GUI are handled by that phase and may change runtime values after startup.

### READ ME button

The **READ ME** button is green with a black outline. It opens this SOP document:

```text
READ ME.md
```

### Close button

The red **Close** button has a black outline and closes the launcher. If a phase is currently running, the launcher first stops that phase process and then closes the GUI. When the launcher closes normally, `START_NPG_CHAMBER.bat` exits without waiting for an extra key press.

Use this button only when you intentionally want to stop the current launcher session. After closing, still check the real hardware state manually: Keysight output/current, PID setpoint, chamber pressure, shutters, leak valve, sputter electronics, and oven state.

On Windows, it should open with the default program associated with Markdown files. It can be opened with Notepad, VS Code, Notepad++, or any text editor.

### Continue-to-next-phase prompt

When one phase exits with code `0`, the launcher asks whether you want to start the next phase:

```text
Heat up + Calibration → Sputtering-Annealing → DP-DBBA Evaporation → NPG Annealings
```

Always confirm the chamber state manually before continuing.

---

## 9. Command-line usage

List available workflows:

```bat
npg-chamber --list
```

Show package version:

```bat
npg-chamber --version
```

Open the old text menu instead of the GUI:

```bat
npg-chamber --text-menu
```

Run a phase directly:

```bat
npg-chamber --run heat
npg-chamber --run sputter
npg-chamber --run dpdbba
npg-chamber --run anneal
```

Run the packaged final scripts explicitly through the legacy/fallback command:

```bat
npg-chamber --run-legacy heat
npg-chamber --run-legacy sputter
npg-chamber --run-legacy dpdbba
npg-chamber --run-legacy anneal
```

Alternative if the `npg-chamber` command is not recognized:

```bat
python -m npg_chamber --version
python -m npg_chamber --list
python -m npg_chamber --run heat
```

---

## 10. Where run data are saved

All run data are now collected under one project folder:

```text
npg_chamber_project/
└─ Data Samples/
   ├─ Heat up + Calibration Data/
   ├─ Sputtering-Annealing Data/
   ├─ DP-DBBA Evaporation Data/
   └─ NPG Annealing Data/
```

Each workflow creates its normal run folder **inside** the matching phase folder. This keeps experimental outputs away from the script folders.

The launcher also sets an environment variable called `NPG_CHAMBER_PHASE_DATA_DIR` before starting a phase. The scripts use it only to choose the data parent folder.

---

## 11. Phase 1 — Heat up + Calibration

Direct command:

```bat
npg-chamber --run heat
```

Purpose:

- heats the CK-1 evaporator;
- monitors QMBs, pressure, oven PID, Keysight, Arduino CK-1 temperature and the IMPAC IPE 140 pyrometer;
- monitors the existing oven PID value but does **not** write the 200 °C Phase 03 startup setpoint;
- controls the Keysight current according to the script logic;
- guides shutter opening/closing;
- performs the calibration step;
- calculates the thickness ratio:

```text
CK-1 relative thickness / Sample relative thickness
```

This ratio is required by the DP-DBBA evaporation phase.

Data parent folder:

```text
Data Samples/Heat up + Calibration Data/
```

Typical run folder inside that parent:

```text
Heat up + Calibration data <run_name>/
```

Typical outputs:

- device data text files;
- graph snapshots;
- phase summaries;
- final thickness ratio information;
- raw pyrometer and calibrated sample-temperature data;
- the run-specific pyrometer profile and three-temperature comparison plots.

Operator notes:

- enter the run name in the launcher GUI before starting;
- follow the script prompts;
- check that the shutter and hardware state match the software state;
- record the ratio for phase 3.

---

## 12. Phase 2 — Sputtering-Annealing

Direct command:

```bat
npg-chamber --run sputter
```

Purpose:

- opens a task-focused Phase 02 operator dashboard with secondary details available on demand;
- controls COSCON directly through UDP instead of embedding the COSCON webpage;
- performs the complete cycle-1 Degas and waits for natural `Standby`;
- validates and applies the 10 mA / 2250 V sputtering target;
- internally verifies COSCON mode, hardware interlock, energy, emission and chamber pressure before the sputtering timer begins;
- continuously supervises COSCON and pressure during sputtering;
- returns COSCON automatically to `Standby` before the leak valve is closed;
- keeps only the manual argon leak-valve open/close confirmations;
- controls and verifies oven PID setpoints for the annealing stages;
- displays current-step time, stage/run elapsed time and known timed work remaining;
- shows one clear Waiting / Ready / Check system result; detailed COSCON mode and interlock values are available only under Auxiliary diagnostics;
- logs chamber, oven, Keysight, COSCON and countdown telemetry to CSV.

Data parent folder:

```text
Data Samples/Sputtering-Annealing Data/
```

Typical run folder inside that parent:

```text
<run_name> Sputtering-Annealing/
```

Typical output file:

```text
sputter_anneal_log.csv
```

Operator notes:

- enter the run name in the launcher GUI before starting;
- keep the COSCON webpage and SpecsLab/Prodigy closed while Phase 02 is active;
- the argon leak valve remains manual and the dashboard shows its buttons only when those actions are required;
- supervise the run and keep access to the local COSCON controls;
- on abort or a detected fault, the script requests a verified COSCON safe state and tries to reset the PID setpoint to 20 °C.

---

## 13. Phase 3 — DP-DBBA Evaporation

Direct command:

```bat
npg-chamber --run dpdbba
```

Purpose:

- uses the run name entered in the launcher GUI;
- uses the thickness ratio automatically captured from Phase 01 when available;
- calculates the DP-DBBA CK-1 evaporation target;
- sets the external oven PID target to 200 °C at startup;
- monitors QMBs, pressure, oven PID, Keysight, CK-1 Arduino temperature and the IMPAC IPE 140 pyrometer;
- guides shutter open/close;
- resets the QMB evaporation window at shutter opening;
- ends according to CK-1 relative thickness target;
- leaves the Keysight in the intended handoff state for NPG Annealings according to the script logic.

Data parent folder:

```text
Data Samples/DP-DBBA Evaporation Data/
```

Typical run folder inside that parent:

```text
DP-DBBA Evaporation data <run_name>/
```

Typical outputs:

- run parameters file;
- device data text files;
- graph snapshots;
- phase summaries;
- raw pyrometer and calibrated sample-temperature data;
- the run-specific pyrometer profile and three-temperature comparison plots.

Operator notes:

- enter the run name in the launcher GUI; the calibration ratio is automatic after Phase 01, or requested by pop-up if the launcher does not know it;
- verify the script target calculation before proceeding;
- when the DP-DBBA target is reached, physically close the shutter and use **Close shutter** or **Finish phase** to confirm the normal handoff. **Finish phase** is blocked before the `WAIT_SHUTTER_CLOSE` stage;
- **Abort / safe stop** remains a different action: it first commands and verifies an oven PID target of 0 °C, then performs the controlled Keysight ramp-down and switches its output off. If the PID write cannot be confirmed, the GUI reports that warning and still continues the electrical safe-stop sequence;
- after normal completion, proceed to NPG Annealings only after confirming the hardware state.

---

## 14. Phase 4 — NPG Annealings

Direct command:

```bat
npg-chamber --run anneal
```

Purpose:

- runs the final NPG annealing sequence;
- controls the oven PID setpoint through the defined stages;
- monitors oven PID temperature;
- monitors the IMPAC IPE 140 raw temperature and calculated sample temperature;
- provides the OVEN PID / PYROMETER / SAMPLE EST. live selector;
- monitors CK-1 Arduino temperature;
- monitors Keysight current and voltage;
- performs the Keysight ramp-down according to the script;
- asks/indicates when the evaporator current must be switched off;
- saves telemetry, plots and database/CSV outputs.

Data parent folder:

```text
Data Samples/NPG Annealing Data/
```

Typical run folder inside that parent:

```text
NPG Annealings <run_name>/
```

Typical outputs:

- SQLite PID temperature database;
- PID temperature CSV;
- telemetry CSV containing oven, raw pyrometer and estimated sample temperatures;
- the run-specific pyrometer profile;
- saved temperature-comparison and electrical plots.

Operator notes:

- enter the run name in the launcher GUI before starting;
- do not leave the system unattended;
- verify current and voltage during ramp-down;
- confirm the evaporator/power state at the end.

---

## 15. Hardware ports used by the scripts

The final scripts use these default COM ports:

| Device | Default port |
|---|---|
| CK-1 evaporator QMB | `COM4` |
| Sample QMB | `COM16` |
| XGS600 HFIG pressure | `COM6` |
| Oven PID temperature | `COM9` |
| Keysight power supply | `COM17` |
| Arduino CK-1 crucible temperature | `COM3` |
| IMPAC IPE 140 pyrometer | `COM10` (38400 baud, 8E1, address `00`) |

Check Windows Device Manager before running the chamber workflow. If Windows assigns different ports, update the relevant script or configuration carefully.

### Automatic COM-port cleanup and phase handoff

Before **every** phase starts, and again immediately after a phase process exits, the launcher verifies all seven chamber COM ports. For each port it:

1. opens the port without transmitting an instrument command;
2. keeps hardware flow-control lines disabled;
3. clears the PC-side serial input and output buffers;
4. closes the handle;
5. confirms that the port can be reopened by the next phase.

The launcher retries busy ports automatically for up to 30 seconds. It will not offer or start the next phase until every configured chamber port is free. If Windows still reports `PermissionError(13, 'Access is denied')`, the GUI identifies the exact blocked port and stops the handoff safely. Close any external program using that port and press the phase **Start** button again; the full check is repeated automatically. A computer restart should therefore be a last-resort action rather than the normal phase-to-phase workflow.

This handoff check does not send QMB, PID, Keysight, XGS600, Arduino, or pyrometer commands and does not change experimental values or hardware safety limits.

---

## 16. Safe checks without hardware

Run package checks:

```bat
python maintenance_tools\run_general_checks.py
```

Run tests directly:

```bat
python -m pytest -q developer_tests
```

Check Python syntax:

```bat
python -m compileall -q npg_chamber developer_tests diagnostic_tools maintenance_tools
```

These checks do not prove hardware behavior. They only confirm that the package, imports, launcher and safe logic are valid.

---

## 17. Optional diagnostic tools

These scripts are available in:

```text
diagnostic_tools/
```

Examples:

```bat
python diagnostic_tools\check_oven_pid_connection.py --port COM9
python diagnostic_tools\check_all_common_devices.py
```

Pyrometer emissivity is configured from the launcher **Pyrometer** tab. At phase startup, the selected value is written only when needed and verified through the instrument's `pa` parameter readback. Close InfraWin and other COM10 users before starting a pyrometer-enabled phase.

Use diagnostic tools only when the hardware state is safe.

---

## 18. Abort and failure behavior

If a script aborts or exits with an error:

1. Check the CMD window.
2. Check the launcher status.
3. Check the chamber pressure.
4. Check the Keysight output and measured current/voltage.
5. Check the oven PID PV/SV.
6. Check the physical shutter/leak valve state.
7. Check whether the script saved a phase summary or snapshot.
8. Do not continue to the next phase until the hardware state is understood.

The GUI will show a warning if a phase exits with a non-zero code.

---

## 19. Fallback to direct scripts

If the launcher does not work, you can run a phase directly:

```bat
python npg_chamber\legacy_scripts\01_heat_up_calibration_legacy.py
python npg_chamber\legacy_scripts\02_sputtering_annealing_legacy.py
python npg_chamber\legacy_scripts\03_dp_dbba_evaporation_legacy.py
python npg_chamber\legacy_scripts\04_npg_annealings_legacy.py
```

The files in `original_scripts_backup/` are for recovery, not routine execution. They match the current runtime scripts when this package is released. Before restoring one later, compare its hash with `SOURCE_CODE_MANIFEST.json` and review `CHANGELOG.md` in case the active copy has subsequently been updated.

---

## 20. Updating the package after changes

After changing package files, reinstall in editable mode:

```bat
python -m pip install -e .
```

Then check:

```bat
npg-chamber --version
npg-chamber --list
```

If installing from a wheel instead of the project folder:

```bat
python -m pip install npg_chamber-0.9.18-py3-none-any.whl
```

For development and laboratory iteration, editable mode is usually easier.

---

## 21. Troubleshooting

### `npg-chamber` is not recognized

Activate the virtual environment:

```bat
.venv\Scripts\activate
```

Or use:

```bat
python -m npg_chamber --version
```

### `-e option requires 1 argument`

You probably ran:

```bat
python -m pip install -e
```

Run this instead:

```bat
python -m pip install -e .
```

### GUI does not open

In version `0.9.4`, the Tkinter startup-order issue that caused this message was fixed:

```text
Too early to create variable: no default root window
```

If the GUI still does not open after updating to version `0.9.7` or later, use the text menu:

```bat
npg-chamber --text-menu
```

Or launch a phase directly:

```bat
npg-chamber --run heat
```

### Serial port error

The launcher automatically retries and resets all configured COM ports before and after each phase. If it blocks a phase, read the exact port name in the GUI/CMD message and check:

- the device is powered;
- the USB/serial cable is connected;
- the COM port is correct;
- no other Python process, terminal, vendor application, Arduino Serial Monitor, or diagnostic tool is using the port;
- Windows Device Manager shows the expected port.

After closing the conflicting application, press the phase **Start** button again. The launcher repeats the full release test; do not continue to the next phase while the warning is present.

### COSCON does not load

Check:

- network connection;
- COSCON IP address;
- browser access to the COSCON URL;
- firewall/network restrictions.

### A phase exits with code other than 0

Stop and inspect the hardware. Do not continue automatically.

---

## 22. Recommended operating sequence

1. Double-click `START_NPG_CHAMBER.bat` in the project folder.

Alternative manual method:

```bat
.venv\Scripts\activate
npg-chamber
```

4. Click **READ ME** if you need the SOP.
5. Start phase 1.
6. Let the phase finish and confirm the hardware state.
7. If appropriate, accept the prompt to continue to phase 2.
8. Repeat for phases 3 and 4.
9. At the end, check saved data and physical hardware state.

---

## 23. Useful files kept outside this SOP

Only non-duplicated support files are kept:

| File | Purpose |
|---|---|
| `CHANGELOG.md` | release history |
| `LICENSE.md` | license/project ownership note |
| `SOURCE_CODE_MANIFEST.json` | exact script hashes |
| `MANIFEST.in` | packaging include rules |
| `pyproject.toml` | Python packaging configuration |

All user instructions are consolidated in this `READ ME.md` file.

## Phase explanation PDF buttons

The graphical launcher includes one explanation button under each Start button:

- `Explanation 01 Heat up + Calibration` opens the Heat up + Calibration PDF explanation.
- `Explanation 02 Sputtering-Annealing` opens the Sputtering-Annealing PDF explanation.
- `Explanation 03 DP-DBBA Evaporation` opens the DP-DBBA Evaporation PDF explanation.
- `Explanation 04 NPG Annealings` opens the NPG Annealings PDF explanation.

These PDF buttons are documentation-only shortcuts. They do not start, stop, or modify any experimental script.
