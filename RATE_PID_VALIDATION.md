# CK-1 rate / compound control — supervised validation checklist

This release adds a new feedback path to Phases 01 and 03. The software paths and controller mathematics are covered by automated tests, but the initial gains have **not** been tuned on the physical chamber. Keep `temperature` as the laboratory default until the following supervised checks have been completed and documented.

## Intended behavior

| Mode | Primary control | Temperature role |
|---|---|---|
| `temperature` | Existing CK-1 temperature PID | PID setpoint and watchdog reference |
| `rate` | Filtered CK-1 QMB rate PID after warm-up | Hard ceiling for positive current corrections and watchdog reference |
| `compound` | Filtered CK-1 QMB rate PID after warm-up | Gradual suppression of positive current corrections near the ceiling; watchdog remains independent |

The rate controller uses conservative asymmetric current limits: it may increase current by at most `0.002 A` per update and reduce it by at most `0.005 A` per update. A stale CK-1 rate signal after handover forces the Keysight to `0 A`, switches the output off and enters `SAFETY_STOP`.

## First supervised test

1. Use a chamber condition and molecule load whose normal temperature/rate response is already known. Keep an operator at the chamber and leave all hardware protections enabled.
2. In **Change automatization parameters**, select `compound`. Start with the packaged target (`0.40 Å/s`), ceiling (`250 °C`) and gains. Do not raise current, voltage or temperature protection limits for this test.
3. Confirm that the GUI initially reports **Warm-up ramp before rate handover**. Rate feedback should not activate below `150 °C`, below the activation rate, or with an old/missing QMB reading.
4. At handover, confirm that the displayed **Active control** changes to **Rate PID**. Check that a measured rate above target lowers current and a rate below target raises it only in small steps.
5. Confirm that the shutter-open prompt appears only after six new filtered-rate readings remain inside the displayed target band.
6. Near the temperature ceiling, verify that positive current corrections become smaller. At or above the ceiling, the controller must not increase current. A high rate must still be allowed to reduce current.
7. During Phase 03 shutter opening, confirm that relative thickness restarts at zero while the rate plot and rate controller remain continuous.
8. End with the normal phase shutdown/handoff and review the saved effective-parameter file, phase summary, QMB data and Keysight current trace.

## Safety-path checks

Perform these only under an approved low-power test condition:

- Interrupt the CK-1 QMB rate stream after rate handover. The run should enter `SAFETY_STOP` after the configured timeout, set current to `0 A` and switch Keysight output off.
- Verify that a CK-1 temperature above the rate-control ceiling plus the existing watchdog margins still produces the established soft reduction and hard stop.
- Confirm that manual current mode pauses both automatic controllers but leaves the watchdogs and electrical hard limits active.

## Gain adjustment rule

Change one parameter at a time and save each tested combination as a named automation mode. Prefer reducing `RATE_PID_KP_A_PER_RATE` or `RATE_PID_MAX_UP_STEP_A` if the rate oscillates. Increase integral action only after proportional-only behavior is stable; keep derivative at zero unless QMB noise and sampling behavior have been characterized. Never compensate an unreachable rate by raising the temperature ceiling without experimental approval.
