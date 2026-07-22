# COSCON IS supervised brief Degas test — corrected for firmware 1.012

## What was corrected

A real supervised test showed that firmware `1.012` reports:

```text
Mode=Degassing
```

rather than `Mode=Degas`.

It also showed that, while Degassing is active:

```text
SwitchToStandby ERROR: Device is degassing.
```

but:

```text
SwitchToOff OK
```

returns the device safely to `Mode=Off`.

This corrected version therefore uses:

```text
Off
→ SwitchToDegas
→ verify Mode=Degassing (or Degas)
→ observe for 10 seconds
→ SwitchToOff
→ verify Mode=Off
```

## Pressure protection

The test reads the XGS600 on `COM6`.

It refuses to start unless:

- COSCON reports `Mode=Off`;
- `Interlock=Ok`;
- three valid pressure readings are available;
- starting pressure is at or below `1e-5 mbar`.

During Degassing it immediately requests `SwitchToOff` if:

- pressure reaches `1e-4 mbar`;
- pressure becomes unavailable or `NaN`;
- COM6 communication fails;
- interlock is not OK;
- COSCON reports Error;
- an unexpected mode or timeout occurs;
- Ctrl+C is pressed.

## Run

Double-click:

```text
RUN_COSCON_DEGAS_TEST.bat
```

Type exactly:

```text
DEGAS TEST
```

when requested.

Reports are saved in:

```text
COSCON Diagnostic Reports
```

## Safe-stop helper

```text
RUN_COSCON_SAFE_STOP.bat
```

requests `SwitchToOff` and verifies `Mode=Off`.

## Strict COSCON command whitelist

- `Info`
- `GetStatus`
- `GetMonitorValues`
- `GetDiagnosticValues`
- `GetTargetValues`
- `SwitchToDegas`
- `SwitchToOff`

No Operate, Reset, network, or preset-write command is available.
