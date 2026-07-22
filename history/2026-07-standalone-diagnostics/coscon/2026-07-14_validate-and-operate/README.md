# COSCON IS validation and supervised Operating tests

These tests use the official UDP interface at:

```text
192.168.236.186:2005
```

Default target values are taken from the detected sputtering preset:

```text
Emission = 0.010 A = 10 mA
Energy   = 2250 V
```

## Test 1 — read-only target validation

Run:

```text
RUN_01_VALIDATE_TARGET.bat
```

Sequence:

```text
GetStatus
→ ValidateOperateTarget Emission=0.01 Energy=2250
→ verify COSCON state did not change
```

This test **cannot activate the source**. Type:

```text
VALIDATE TARGET
```

when requested.

Do not run Test 2 unless Test 1 succeeds.

## Test 2 — supervised active Operating test

Run:

```text
RUN_02_OPERATE_TEST.bat
```

Default sequence:

```text
Mode=Off
→ validate 10 mA / 2250 V
→ SwitchToOperate
→ verify Mode=Operating
→ hold 5 seconds
→ SwitchToStandby
→ verify Mode=Standby
→ SwitchToOff
→ verify Mode=Off
```

### Physical requirements

A trained operator must be present and verify:

1. A complete Degas cycle has been performed.
2. The sputter-gun cable is correctly connected and oriented.
3. The manual argon valve is open.
4. Pressure is stable near `2e-5 mbar`.
5. Sample/shutter arrangement is safe for a brief sputtering pulse.
6. COSCON has no visible fault.
7. No other client sends COSCON commands.
8. No other program uses XGS600 `COM6`.

The script requires three valid pressure readings in:

```text
1e-5 mbar <= pressure <= 5e-5 mbar
```

and continuously monitors pressure and interlock while Operating.

Type exactly:

```text
OPERATE 10mA 2250V
```

to authorize high voltage.

### Safety stop

On pressure loss/out-of-range, interlock change, error, timeout, unexpected mode,
or Ctrl+C, the program attempts:

```text
SwitchToStandby
→ verify Standby or Off
→ SwitchToOff
→ verify Off
```

Because UDP/network communication can fail, this cannot replace local physical
controls.

## Separate safe-stop helper

```text
RUN_COSCON_SAFE_STOP.bat
```

does not activate the source. It requests Standby and then Off.

## Reports

Both tests save timestamped reports in:

```text
COSCON Diagnostic Reports
```

## Important

The argon leak valve remains manual. These scripts do not control it.
