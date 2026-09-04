"""Run the packaged phase scripts from one unified entry point."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from npg_chamber import __version__, __build__
from npg_chamber.common.paths import phase_script_dir, phase_data_dir
from npg_chamber.common.serial_handoff import (
    SerialHandoffError,
    verify_all_chamber_ports_released,
)


@dataclass(frozen=True)
class Workflow:
    key: str
    title: str
    filename: str

    @property
    def path(self) -> Path:
        return phase_script_dir() / self.filename

    @property
    def data_dir(self) -> Path:
        return phase_data_dir(self.key)


WORKFLOWS: dict[str, Workflow] = {
    "heat": Workflow(
        key="heat",
        title="Heat up + Calibration",
        filename="01_heat_up_calibration.py",
    ),
    "sputter": Workflow(
        key="sputter",
        title="Sputtering-Annealing",
        filename="02_sputtering_annealing.py",
    ),
    "dpdbba": Workflow(
        key="dpdbba",
        title="DP-DBBA Evaporation",
        filename="03_dp_dbba_evaporation.py",
    ),
    "anneal": Workflow(
        key="anneal",
        title="NPG Annealings",
        filename="04_npg_annealings.py",
    ),
}


def list_workflows() -> str:
    lines = ["Available workflows:"]
    for key, workflow in WORKFLOWS.items():
        lines.append(f"  {key:8s} - {workflow.title}")
    return "\n".join(lines)


def _workflow_for_key(key: str) -> Workflow:
    try:
        workflow = WORKFLOWS[key]
    except KeyError as exc:
        valid = ", ".join(sorted(WORKFLOWS))
        raise ValueError(f"Unknown workflow {key!r}. Valid values: {valid}") from exc

    if not workflow.path.exists():
        raise FileNotFoundError(f"Workflow script not found: {workflow.path}")
    return workflow


def workflow_environment(workflow: Workflow, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment used for a packaged workflow process.

    Every phase is started as a standalone script with its data folder as the
    working directory. Add the current project root to ``PYTHONPATH`` so the
    child process imports this copy of ``npg_chamber`` regardless of where the
    project was extracted.
    """

    data_dir = workflow.data_dir
    env = os.environ.copy()
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})

    project_root = str(Path(__file__).resolve().parents[2])
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    pythonpath_entries = [
        entry
        for entry in existing_pythonpath.split(os.pathsep)
        if entry.strip()
    ]
    pythonpath_entries = [
        entry
        for entry in pythonpath_entries
        if os.path.normcase(os.path.abspath(entry))
        != os.path.normcase(os.path.abspath(project_root))
    ]
    env["PYTHONPATH"] = os.pathsep.join([project_root, *pythonpath_entries])

    env["NPG_CHAMBER_WORKFLOW_KEY"] = workflow.key
    env["NPG_CHAMBER_WORKFLOW_TITLE"] = workflow.title
    env["NPG_CHAMBER_PHASE_DATA_DIR"] = str(data_dir)
    env["NPG_CHAMBER_DATA_ROOT"] = str(data_dir.parent)
    env["NPG_CHAMBER_UNIFIED_LAUNCHER"] = "1"
    return env




def serial_release_delay_s() -> float:
    """Small Windows-friendly pause after a phase exits.

    Some USB-RS232 drivers release a COM handle a little after the Python process
    has already returned. This does not change any phase control logic; it only
    avoids starting the next phase while Windows is still freeing COM ports.
    """

    raw = os.environ.get("NPG_CHAMBER_PORT_RELEASE_DELAY_S", "2.0").strip()
    try:
        value = float(raw)
    except Exception:
        value = 2.0
    return max(0.0, value)


def wait_for_serial_release_after_phase(key: str | None = None) -> None:
    delay_s = serial_release_delay_s()
    if delay_s <= 0:
        return
    label = f" after {key}" if key else ""
    print(f"Waiting {delay_s:.1f} s for Windows to release COM ports{label} ...")
    time.sleep(delay_s)


def wait_for_phase_process(process: subprocess.Popen, key: str | None = None) -> int:
    """Wait for a phase and verify that every chamber COM port is reusable.

    A successful child-process exit is not considered a complete phase handoff
    until all configured chamber ports can be opened, have their PC-side buffers
    cleared, and be closed again.  If this verification fails, the caller receives
    :class:`SerialHandoffError` and must not start the next phase.
    """

    exit_code = int(process.wait())
    wait_for_serial_release_after_phase(key)
    label = f"after phase {key}" if key else "after phase process"
    verify_all_chamber_ports_released(context=label)
    return exit_code


def launch_workflow_process(
    key: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start one workflow script and return the running process handle.

    The experiment script owns all instrument-control and safe-stop logic.
    The launcher only starts and waits for the child process; it deliberately
    does not force-terminate an active phase.
    """

    workflow = _workflow_for_key(key)
    data_dir = workflow.data_dir
    env = workflow_environment(workflow, extra_env=extra_env)

    # Verify the complete chamber serial set before every phase, including the
    # first one.  This catches stale handles from a previous launcher session or
    # another program before the experimental script begins opening instruments.
    verify_all_chamber_ports_released(context=f"before phase {key}")

    print(f"Software build: v{__version__} ({__build__})")
    print(f"Launching: {workflow.title}")
    print(f"Script: {workflow.path}")
    print(f"Data folder: {data_dir}")

    creationflags = 0
    start_new_session = False
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        start_new_session = True

    return subprocess.Popen(
        [sys.executable, str(workflow.path)],
        cwd=str(data_dir),
        env=env,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )



def run_workflow(key: str, extra_env: dict[str, str] | None = None) -> int:
    """Run one workflow script as a separate Python process."""

    try:
        process = launch_workflow_process(key, extra_env=extra_env)
        return wait_for_phase_process(process, key)
    except SerialHandoffError as exc:
        print(f"\n{exc}\n")
        return 90
