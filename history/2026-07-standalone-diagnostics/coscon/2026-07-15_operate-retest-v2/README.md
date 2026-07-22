# COSCON IS supervised Operating re-test v2

Use this version instead of the previous active-test script.

## Improvements

- Accepts initial `Mode=Off` or `Mode=Standby`.
- Correctly interprets `GetStatus OK: Mode=Error ...` as a device fault, not a rejected query.
- Logs pressure plus target, monitor and diagnostic values during the HV ramp.
- Captures a full diagnostic snapshot immediately if `Mode=Error` appears.
- Extends the ramp timeout to 45 seconds.
- Uses a shorter 3-second Operating hold.
- Never blindly retries `SwitchToOperate`.
- Does not include Reset, network or preset-write commands.

## Target kept unchanged

```text
Emission = 0.010 A = 10 mA
Energy   = 2250 V
```

The script does not experiment with lower or higher values.

## Before running

Only run after the equipment inspection and with a trained person present. Confirm:

1. Complete Degas performed.
2. Cable/feedthrough checked.
3. Argon pressure stable near `2e-5 mbar`.
4. Sample and shutter safe for a 3-second pulse.
5. No web UI, SpecsLab or Phase 2 command is being sent.
6. Physical COSCON controls immediately accessible.

## Run

```text
RUN_COSCON_OPERATE_RETEST_V2.bat
```

Type exactly:

```text
OPERATE RETEST 10mA 2250V
```

The allowed pressure window remains:

```text
1e-5 mbar <= pressure <= 5e-5 mbar
```

## Planned sequence

```text
Off or Standby
→ ValidateOperateTarget
→ SwitchToOperate
→ verify Operating
→ monitor for 3 seconds
→ SwitchToStandby
→ verify Standby
→ SwitchToOff
→ verify Off
```

Reports are saved under `COSCON Diagnostic Reports`.
