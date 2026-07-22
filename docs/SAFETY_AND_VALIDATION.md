# Safety and validation

## Safety position

This software is a supervised research control application. It is not a certified safety controller or safety PLC and must not be treated as the only protective layer.

The chamber's hardware interlocks, instrument-internal limits, power-supply protections, pressure limits, laboratory procedures, and trained operator remain authoritative. The operator must stay present and verify the physical state before, during, and after every phase.

## Software safety mechanisms

The integrated package includes, among other checks:

- validation of editable run parameters before phase launch;
- exclusion of communication settings and hard electrical limits from routine recipe editing;
- pressure, current, voltage, temperature, and workflow-state checks inside the relevant phase;
- watchdog and controlled ramp-down behavior where applicable;
- explicit distinction between normal Phase 03 completion and Abort / safe stop;
- pre- and post-phase COM-port release verification;
- source hashes for the authoritative scripts and recovery copies;
- run-local records of the effective automation parameters.

These mechanisms reduce foreseeable software and workflow errors. They do not prove that the entire chamber is safe under every hardware failure.

## Validation evidence

The codebase promoted to `0.9.18` recorded 93 passing tests without chamber hardware on 21 July 2026. The suite covers imports and packaging, GUI behavior, documentation consistency, parameter validation, automation modes, serial handoff, pyrometer parsing and profiles, Phase 02 dashboard logic, Phase 01/03 workflow separation, and source-manifest integrity.

Supervised laboratory work also included instrument communication checks, real PID and QMB acquisition, an NPG annealing run, COSCON Degas and Operate trials, pyrometer communication and emissivity verification, and repeated complete-workflow trials that exposed the original COM-port handoff failures.

## Remaining acceptance work

Before describing the system publicly as fully validated, the laboratory should retain signed evidence for:

1. command-by-command COSCON protocol checks with expected replies;
2. loss-of-communication and stale-data behavior;
3. pressure and interlock fault injection;
4. abort from every reachable Phase 02 state;
5. at least three complete supervised recipe cycles with post-run hardware inspection;
6. verification on the exact Windows workstation and instrument configuration used for normal operation.

Public documentation should say "software regression tested and experimentally exercised" unless this complete hardware acceptance record exists.
