# COSCON manual-process read-only logger

This tool records the COSCON and XGS600 while the operator performs the normal
sputtering procedure manually.

## It cannot control the equipment

The strict COSCON whitelist contains only:

- `Info`
- `GetStatus`
- `GetTargetValues`
- `GetMonitorValues`
- `GetDiagnosticValues`

It contains no Degas, Operate, Standby, Off, Reset, network, or preset-write
command.

## Why run it

The previous supervised Python tests reached `Operating`, but the measured
energy remained far below the `2250 V` target and the COSCON then reported:

```text
Error: HV-Module Energy Overload
```

This logger will show whether the same thing happens during the usual manual
procedure or whether the manual interface uses a different sequence.

## Before starting

- Use one manual control client only: the COSCON web page **or** SpecsLab.
- Do not run Phase 2 simultaneously.
- Make sure no other program is using the XGS600 COM port.
- A trained operator remains responsible for the physical process and stopping
  the equipment.

## Run

Double-click:

```text
RUN_COSCON_MANUAL_READONLY_LOGGER.bat
```

Type exactly:

```text
START READ ONLY LOGGER
```

Then perform the process manually. When it ends or an error appears, return to
the logger window and press `ENTER`.

## Output

The folder:

```text
COSCON Manual Logger Reports
```

will contain:

- `*_summary.txt` — main result and maxima;
- `*_snapshots.csv` — timestamped COSCON state and telemetry;
- `*_pressure.csv` — high-frequency pressure;
- `*_raw.log` — every command and reply.

## How to interpret it

- Manual operation also reaches `HV-Module Energy Overload`:
  the failure is not caused by the Python activation command.
- Manual operation remains `Operating` and measured `VEnergy` approaches
  `2250 V`:
  the manual interface/process is doing something different.
- `Mode=Operating` appears but `VEnergy` collapses and an error follows:
  the source did not reach stable operation, even if `Operating` appeared
  briefly.
