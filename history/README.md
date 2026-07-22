# Historical source archive

This directory preserves source-backed stages and an explicit record of files that are known from the project conversations but have not yet been recovered.

## `2026-03-initial-prototypes`

- `multi_instrument_monitoring_prototype.py` is the early real-time acquisition script for the chamber instruments.
- `npg_annealing_recipe_prototype.ipynb` is the early PID-driven staged annealing notebook. It retains historical execution output and must remain private until the experimental data are approved for publication.

## `diagnostic-prototypes`

- `pid_setpoint_write_test.py` is a controlled RKC PID setpoint write/readback diagnostic. Its user-facing text was translated into English for repository consistency; its safety confirmation and protocol logic were not intentionally changed.

## `2026-07-recovered-packages`

- Sixteen complete source-backed project ZIPs cover the development from `0.9.11` through the last recovered `0.9.17` state.
- Each ZIP has a curated browsable view containing the main phase scripts and the launcher, configuration and device modules that changed during this period.
- The chronological table and archive hashes are in [Recovered July project packages](2026-07-recovered-packages/README.md).

## `2026-07-standalone-diagnostics`

- Preserves the pyrometer tests and the staged COSCON Identify, Standby, Degas, Operate and read-only logger utilities used before integration.
- These files can communicate with chamber hardware and are retained as evidence, not as supported operator entry points.

## Files still to recover

[Known conversation files not yet recovered](MISSING_SOURCE_FILES.md) lists the exact April–June filenames confirmed by the chats but not available as source bytes in the current archive. They are not reconstructed from descriptions.

Everything under `history/` is historical evidence, not the supported runtime. Use the implementations under `npg_chamber/` for the integrated project.
