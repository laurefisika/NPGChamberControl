# COSCON UDP Operating verification v3

Use this test after the successful manual run.

## Why this version is different

The successful manual logger showed:

- complete Degas ended naturally in `Standby`;
- the source stayed in Standby while argon was prepared;
- `SwitchingToOperate` lasted about 10.5 seconds;
- once `Operating` was reached, measured energy reached about 2250 V almost
  immediately;
- measured emission remained near 10 mA;
- no COSCON error appeared;
- the normal manual finish returned to `Standby`.

This test reproduces that sequence more closely.

## Normal sequence

```text
natural Standby after complete Degas
→ 60 s verified Standby + stable pressure conditioning
→ ValidateOperateTarget 10 mA / 2250 V
→ SwitchToOperate once
→ wait for Operating
→ require five consecutive valid measured samples
→ 60 s stable Operating hold
→ SwitchToStandby
→ leave COSCON in Standby
→ operator closes argon valve
```

The test does not treat `Mode=Operating` alone as success. It also requires:

```text
VEnergy = 2250 ± 50 V
IEmission = 10 ± 1 mA
pressure = 1e-5 to 5e-5 mbar
Interlock = Ok
```

## Before running

1. Perform a complete Degas manually.
2. Let Degas finish naturally in `Standby`.
3. Do **not** place the COSCON in Off.
4. Open argon and stabilize the pressure near `2e-5 mbar`.
5. Check the source cable, connector and feedthrough.
6. Keep the sample and shutter in a safe position.
7. Close Phase 2 and any program using COM6.
8. Do not use the web interface or SpecsLab to send COSCON commands while this
   test is active.
9. Keep the local physical controls accessible.

## Run

Double-click:

```text
RUN_COSCON_UDP_OPERATE_TEST_V3.bat
```

Type exactly:

```text
START UDP SPUTTER TEST 10mA 2250V
```

The script first performs 60 seconds of Standby and pressure verification. Only
after that does it send the active command.

At the normal end, it returns to `Standby` and asks you to close the manual
argon valve. Type:

```text
ARGON VALVE CLOSED
```

after physically closing it.

## Fault response

The script records a complete diagnostic snapshot and attempts:

```text
SwitchToStandby
```

It requests `SwitchToOff` only if Standby or Off cannot be confirmed.

It never sends `Reset` and never blindly retries `SwitchToOperate`.

## Separate safe-stop helper

```text
RUN_COSCON_SAFE_STOP_V3.bat
```

does not activate the source.

## Reports

The folder:

```text
COSCON UDP Test Reports
```

contains:

- `*_summary.txt`
- `*_telemetry.csv`
- `*_raw.log`
