# Contributing to NPG Chamber Controller

Thank you for helping maintain the NPG Chamber Controller. This repository
contains research-control software for laboratory hardware, so every change
must be reviewable, reproducible and conservative around instrument safety.

## Before opening a change

1. Read `README.md`, `CHANGELOG.md` and `LICENSE.md`.
2. Never commit credentials, local configuration, operator data, instrument
   exports or chamber logs.
3. Do not test against the real chamber as part of an automated workflow.
   Hardware tests require a supervised laboratory procedure.
4. Keep phase behaviour, safety limits and handoff semantics explicit in the
   change description.

## Local development

Create an isolated environment and install the development dependencies:

```text
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the same checks used by continuous integration:

```text
python -m compileall -q npg_chamber diagnostic_tools maintenance_tools developer_tests
python -m pytest -q
```

Before packaging a phase-script change, also run:

```text
python -m npg_chamber.installation_check --expected-build 2026.09.04-r20
```

## Change guidelines

- Prefer small, focused commits with descriptive messages.
- Do not edit generated run data into the source tree.
- Keep the phase explanation PDF, README defaults, changelog and source
  manifest synchronized when an operator-visible behaviour changes.
- Preserve the phase-specific safe-stop path and avoid force-killing hardware
  processes.
- Add or update hardware-independent regression tests for every behavioural
  change that can be tested without instruments.

Pull requests should explain the motivation, affected phase(s), safety impact,
tests executed and any remaining hardware-validation requirement. Publication,
redistribution and reuse remain subject to the terms in `LICENSE.md` and the
permissions of the relevant rights holders.
