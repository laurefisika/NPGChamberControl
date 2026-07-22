# Historical source archive

This directory preserves source-backed stages that predate the integrated package.

## `2026-03-initial-prototypes`

- `multi_instrument_monitoring_prototype.py` is the early real-time acquisition script for the chamber instruments.
- `npg_annealing_recipe_prototype.ipynb` is the early PID-driven staged annealing notebook. It retains historical execution output and must remain private until the experimental data are approved for publication.

## `diagnostic-prototypes`

- `pid_setpoint_write_test.py` is a controlled RKC PID setpoint write/readback diagnostic. Its user-facing text was translated into English for repository consistency; its safety confirmation and protocol logic were not intentionally changed.

These files are historical evidence, not the supported runtime. Use the implementations under `npg_chamber/` for the integrated project.

