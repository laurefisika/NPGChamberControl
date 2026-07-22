# Standalone July diagnostics

These source-backed utilities document the supervised tests that preceded the integrated pyrometer and COSCON implementations.

## Pyrometer

The `pyrometer/` folder preserves the initial IMPAC IPE 140 connection tests, the COM10-specific test and the live COM10 monitor.

## COSCON

The `coscon/` folder preserves each separate test package in chronological order:

1. Identify on/off without changing operating mode.
2. Standby and Off handling.
3. Two revisions of the supervised Degas test.
4. Target validation and supervised Operate.
5. A second supervised Operate test.
6. A read-only manual logger.
7. The final UDP Operate test and safe-stop helper used before integration.

These are historical hardware-control tools, not automated tests and not the supported Phase 02 runtime. Several scripts can command Degas, Operate, Standby or Off. They must only be used on the intended chamber by a trained operator under the laboratory procedure and hardware interlocks.

