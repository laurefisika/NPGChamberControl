# COSCON IS supervised brief Degas test

## Purpose

This is a **brief transition test**, not a complete degassing procedure.

Default sequence:

```text
Off
→ SwitchToDegas
→ verify Mode=Degas
→ observe for 10 seconds
→ SwitchToStandby
→ verify Standby or Off
→ SwitchToOff
→ verify Mode=Off
```

Pressure is read automatically from the XGS600 on `COM6`.

## Safety rules built into the script

The program refuses to start unless:

- COSCON reports `Mode=Off`;
- COSCON reports `Interlock=Ok`;
- three valid pressure readings are obtained;
- the highest starting pressure is at or below `1e-5 mbar`.

During Degas, the program immediately starts the documented safe-stop sequence if:

- pressure reaches `1e-4 mbar`;
- the pressure value becomes unavailable or `NaN`;
- the XGS600 serial connection fails;
- COSCON interlock is no longer `Ok`;
- COSCON reports `Error`;
- an unexpected COSCON mode appears;
- a timeout or communication error occurs;
- the user presses `Ctrl+C`.

Safe-stop sequence:

```text
SwitchToStandby
→ SwitchToOff
→ verify Mode=Off
```

The automatic safe stop cannot replace the physical COSCON controls if network,
power, serial communication, or the device itself has failed.

## Before running

A trained operator must be physically present and verify:

1. The IQE 11/35 cable is correctly oriented and connected.
2. The chamber and source are under suitable vacuum.
3. The argon leak valve is closed.
4. COSCON shows no fault/interlock alarm.
5. SpecsLab, the COSCON web interface and Phase 2 will not send commands.
6. Local COSCON controls are immediately accessible.
7. No other program is using `COM6`.

## Run

Double-click:

```text
RUN_COSCON_DEGAS_TEST.bat
```

The program will show the measured pressure and require the exact confirmation:

```text
DEGAS TEST
```

Reports are saved under:

```text
COSCON Diagnostic Reports
```

## Safe-stop helper

Double-click:

```text
RUN_COSCON_SAFE_STOP.bat
```

It does not start Degas. It requests Standby and then Off, and verifies
`Mode=Off`.

## Strict COSCON command whitelist

The Python program accepts only:

- `Info`
- `GetStatus`
- `GetMonitorValues`
- `GetDiagnosticValues`
- `GetTargetValues`
- `SwitchToDegas`
- `SwitchToStandby`
- `SwitchToOff`

It cannot send Operate, Reset, network configuration, or preset-write commands.
