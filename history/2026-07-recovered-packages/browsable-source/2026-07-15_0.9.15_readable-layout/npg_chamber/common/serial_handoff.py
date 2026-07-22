"""Verified serial-port handoff between NPG chamber phases.

The four experimental phases run in separate Python processes.  On Windows,
a process can finish before a USB/RS-232 driver has fully released its COM
handle.  A fixed sleep helps, but it does not prove that the port is available.

This module performs an active, non-commanding handoff check.  It opens each
configured chamber port with hardware flow control disabled, keeps DTR/RTS low,
clears the PC-side input/output buffers, closes the port, and retries until all
ports can be opened successfully or a timeout is reached.

No instrument command is transmitted by this module and no experimental or
safety setpoint is changed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from npg_chamber.config.ports import DEFAULT_PORTS, SerialPortConfig

try:  # pragma: no cover - availability depends on the runtime environment
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]


@dataclass(frozen=True)
class NamedSerialPort:
    """Human-readable name plus the port settings used for a handoff check."""

    name: str
    config: SerialPortConfig


ALL_CHAMBER_PORTS: tuple[NamedSerialPort, ...] = (
    NamedSerialPort("CK-1 evaporator QMB", DEFAULT_PORTS.ck1_qmb),
    NamedSerialPort("Sample QMB", DEFAULT_PORTS.sample_qmb),
    NamedSerialPort("XGS600 HFIG pressure", DEFAULT_PORTS.xgs600),
    NamedSerialPort("Oven PID temperature", DEFAULT_PORTS.oven_pid),
    NamedSerialPort("Keysight power supply", DEFAULT_PORTS.keysight),
    NamedSerialPort("Arduino CK-1 crucible temperature", DEFAULT_PORTS.ck1_arduino),
)


class SerialLike(Protocol):
    is_open: bool
    port: str | None

    def open(self) -> None: ...

    def close(self) -> None: ...

    def reset_input_buffer(self) -> None: ...

    def reset_output_buffer(self) -> None: ...


SerialFactory = Callable[[NamedSerialPort], SerialLike]


class SerialHandoffError(RuntimeError):
    """Raised when one or more chamber ports remain unavailable after retries."""

    def __init__(self, context: str, failures: dict[str, str], timeout_s: float) -> None:
        self.context = context
        self.failures = dict(failures)
        self.timeout_s = float(timeout_s)
        lines = [
            f"Serial handoff failed {context} after {timeout_s:.1f} s.",
            "The next phase was not started because these COM ports are still unavailable:",
        ]
        for label, detail in self.failures.items():
            lines.append(f"- {label}: {detail}")
        lines.append(
            "Close any other program using these ports and press the phase Start button again; "
            "the launcher will automatically retry the full check."
        )
        super().__init__("\n".join(lines))


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(minimum, value)


def handoff_timeout_s() -> float:
    """Maximum time spent waiting for every COM port to become available."""

    return _env_float("NPG_CHAMBER_PORT_HANDOFF_TIMEOUT_S", 30.0, 1.0)


def retry_interval_s() -> float:
    return _env_float("NPG_CHAMBER_PORT_RETRY_INTERVAL_S", 0.75, 0.05)


def post_close_settle_s() -> float:
    return _env_float("NPG_CHAMBER_PORT_POST_CLOSE_SETTLE_S", 0.35, 0.0)


def _default_serial_factory(spec: NamedSerialPort) -> SerialLike:
    if serial is None:  # pragma: no cover - pyserial is a package dependency
        raise ImportError("pyserial is required. Install it with: pip install pyserial")

    # Build the object while closed so DTR/RTS can be kept low before Windows
    # opens the handle.  This avoids intentionally toggling control lines or
    # sending any command to the connected instrument during the check.
    ser = serial.Serial(
        port=None,
        baudrate=spec.config.baudrate,
        timeout=0.25,
        write_timeout=0.25,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    try:
        ser.dtr = False
    except Exception:
        pass
    try:
        ser.rts = False
    except Exception:
        pass
    ser.port = spec.config.port
    ser.open()
    return ser


def reset_and_release_port(
    spec: NamedSerialPort,
    *,
    serial_factory: SerialFactory | None = None,
) -> None:
    """Open, clear PC-side buffers, and close one port without sending commands."""

    factory = serial_factory or _default_serial_factory
    ser: SerialLike | None = None
    try:
        ser = factory(spec)
        if not getattr(ser, "is_open", True):
            ser.open()
        try:
            ser.reset_input_buffer()
        except Exception:
            # Some test doubles or unusual drivers may not implement buffer reset.
            # Successful exclusive opening still proves the previous handle is gone.
            pass
        try:
            ser.reset_output_buffer()
        except Exception:
            pass
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


def verify_all_chamber_ports_released(
    *,
    context: str,
    ports: Iterable[NamedSerialPort] = ALL_CHAMBER_PORTS,
    timeout_s: float | None = None,
    retry_s: float | None = None,
    serial_factory: SerialFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[NamedSerialPort, ...]:
    """Wait until every configured chamber port can be reset and released.

    The function retries only the ports that failed on the previous pass.  It
    returns the verified ports.  If any port remains unavailable at the timeout,
    :class:`SerialHandoffError` is raised and the launcher must not continue.
    """

    selected = tuple(ports)
    if not selected:
        return selected

    timeout = handoff_timeout_s() if timeout_s is None else max(0.0, float(timeout_s))
    retry = retry_interval_s() if retry_s is None else max(0.01, float(retry_s))
    deadline = time.monotonic() + timeout
    pending = {spec.config.port.upper(): spec for spec in selected}
    failures: dict[str, str] = {}
    pass_number = 0

    print(f"Serial handoff check {context}: verifying {len(selected)} chamber COM ports ...")

    while pending:
        pass_number += 1
        for key, spec in list(pending.items()):
            try:
                reset_and_release_port(spec, serial_factory=serial_factory)
            except Exception as exc:
                failures[key] = f"{spec.name} ({spec.config.port}, {spec.config.baudrate} baud): {exc}"
            else:
                pending.pop(key, None)
                failures.pop(key, None)
                print(
                    f"  OK: {spec.name} ({spec.config.port}) opened, buffers cleared, and closed."
                )

        if not pending:
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        if pass_number == 1 or pass_number % 4 == 0:
            waiting = ", ".join(sorted(pending))
            print(f"  Waiting for Windows to release: {waiting}")
        sleep(min(retry, max(0.0, remaining)))

    if pending:
        final_failures = {
            key: failures.get(
                key,
                f"{spec.name} ({spec.config.port}) remained unavailable",
            )
            for key, spec in pending.items()
        }
        raise SerialHandoffError(context, final_failures, timeout)

    settle = post_close_settle_s()
    if settle > 0:
        sleep(settle)
    print(f"Serial handoff check {context}: all chamber COM ports are free.\n")
    return selected
