"""Run the packaged workflow scripts from one unified entry point."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from npg_chamber.common.paths import legacy_dir, phase_data_dir
from npg_chamber.common.serial_handoff import (
    SerialHandoffError,
    verify_all_chamber_ports_released,
)


@dataclass(frozen=True)
class LegacyWorkflow:
    key: str
    title: str
    filename: str

    @property
    def path(self) -> Path:
        return legacy_dir() / self.filename

    @property
    def data_dir(self) -> Path:
        return phase_data_dir(self.key)


LEGACY_WORKFLOWS: dict[str, LegacyWorkflow] = {
    "heat": LegacyWorkflow(
        key="heat",
        title="Heat up + Calibration",
        filename="01_heat_up_calibration_legacy.py",
    ),
    "sputter": LegacyWorkflow(
        key="sputter",
        title="Sputtering-Annealing",
        filename="02_sputtering_annealing_legacy.py",
    ),
    "dpdbba": LegacyWorkflow(
        key="dpdbba",
        title="DP-DBBA Evaporation",
        filename="03_dp_dbba_evaporation_legacy.py",
    ),
    "anneal": LegacyWorkflow(
        key="anneal",
        title="NPG Annealings",
        filename="04_npg_annealings_legacy.py",
    ),
}


def list_workflows() -> str:
    lines = ["Available workflows:"]
    for key, workflow in LEGACY_WORKFLOWS.items():
        lines.append(f"  {key:8s} - {workflow.title}")
    return "\n".join(lines)


def _workflow_for_key(key: str) -> LegacyWorkflow:
    try:
        workflow = LEGACY_WORKFLOWS[key]
    except KeyError as exc:
        valid = ", ".join(sorted(LEGACY_WORKFLOWS))
        raise ValueError(f"Unknown workflow {key!r}. Valid values: {valid}") from exc

    if not workflow.path.exists():
        raise FileNotFoundError(f"Workflow script not found: {workflow.path}")
    return workflow


def workflow_environment(workflow: LegacyWorkflow, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment used for a packaged workflow process."""

    data_dir = workflow.data_dir
    env = os.environ.copy()
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
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


def launch_legacy_workflow_process(
    key: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start one workflow script and return the running process handle.

    This is used by the GUI launcher so the Close button can stop a running
    phase before closing the GUI/CMD window. The experiment script itself still
    owns all instrument-control logic; this function only starts the process.
    """

    workflow = _workflow_for_key(key)
    data_dir = workflow.data_dir
    env = workflow_environment(workflow, extra_env=extra_env)

    # Verify the complete chamber serial set before every phase, including the
    # first one.  This catches stale handles from a previous launcher session or
    # another program before the experimental script begins opening instruments.
    verify_all_chamber_ports_released(context=f"before phase {key}")

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


def terminate_process(process: subprocess.Popen, timeout_s: float = 5.0) -> int | None:
    """Stop a workflow process and its GUI/child processes as reliably as possible."""

    if process.poll() is not None:
        return process.returncode

    if sys.platform.startswith("win"):
        # Matplotlib/pywebview windows may live in child processes. taskkill /T /F
        # prevents the unified launcher from hanging when Close or Ctrl+C is used.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(2.0, timeout_s),
                check=False,
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass

        deadline = time.time() + max(0.1, timeout_s)
        while time.time() < deadline:
            if process.poll() is not None:
                return process.returncode
            time.sleep(0.1)

        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    try:
        return process.wait(timeout=2.0)
    except Exception:
        return process.returncode


def run_legacy_workflow(key: str, extra_env: dict[str, str] | None = None) -> int:
    """Run one workflow script as a separate Python process."""

    try:
        process = launch_legacy_workflow_process(key, extra_env=extra_env)
        return wait_for_phase_process(process, key)
    except SerialHandoffError as exc:
        print(f"\n{exc}\n")
        return 90
