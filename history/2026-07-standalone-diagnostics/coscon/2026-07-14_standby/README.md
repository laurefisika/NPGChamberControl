# COSCON IS supervised Standby test

This package performs only:

`Off → Standby → Off`

It uses the official COSCON IS UDP interface at `192.168.236.186:2005`.

## Before running

A trained operator must confirm:

1. IQE 11/35 and COSCON cable correctly connected.
2. Chamber under the vacuum conditions required by the laboratory SOP.
3. COSCON has no fault/interlock alarm.
4. No other software or person will command the COSCON during the test.
5. Argon leak valve closed.
6. Local controls are accessible.

## Run

Double-click:

`RUN_COSCON_STANDBY_TEST.bat`

The program first verifies `Mode=Off` and `Interlock=OK`. It will then require
the exact phrase:

`STANDBY TEST`

The default Standby hold is 10 seconds.

Reports are saved under:

`COSCON Diagnostic Reports`

## Emergency helper

`RUN_COSCON_OFF_ONLY.bat` requests `SwitchToOff` and verifies `Mode=Off`.

This helper cannot replace local intervention if communication, power, or the
COSCON itself has failed.

## Strict command limit

The Python file accepts only:

- Info
- GetStatus
- GetMonitorValues
- GetDiagnosticValues
- GetTargetValues
- SwitchToStandby
- SwitchToOff

It cannot send Degas, Operate, Reset, network, or preset-write commands.
