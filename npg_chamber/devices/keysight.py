"""Keysight E3632A power supply helper.

This module centralises the small SCPI operations that were repeated across the
the chamber phase scripts. It intentionally keeps the same simple command style as
those scripts: commands are sent as text lines terminated by ``\n`` and queries
are read with ``readline()``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

try:  # pragma: no cover - import availability depends on the runtime computer
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

from npg_chamber.config.ports import DEFAULT_PORTS, SerialPortConfig


class KeysightError(RuntimeError):
    """Base exception for Keysight communication errors."""


class KeysightSafetyError(KeysightError):
    """Raised when a command is rejected by a software safety guard."""


def parse_optional_float(text: str) -> Optional[float]:
    """Parse a float from a SCPI response, returning ``None`` on failure."""

    if text is None:
        return None
    stripped = str(text).strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


@dataclass(frozen=True)
class ProtectionStatus:
    """Snapshot of output/protection latch states."""

    output_on: Optional[bool]
    ocp_tripped: Optional[bool]
    ovp_tripped: Optional[bool]


@dataclass(frozen=True)
class VoltageCurrentReadback:
    """Keysight voltage/current measurement pair."""

    voltage_v: Optional[float]
    current_a: Optional[float]


class KeysightE3632A:
    """Small SCPI wrapper for the Keysight E3632A used in the chamber.

    The class is intentionally conservative. It does not impose experimental
    limits by itself unless the caller passes a cap/limit argument to one of the
    safety-aware methods. This prevents hidden changes to the workflow values.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout_s: float = 1.0,
        *,
        serial_instance: object | None = None,
    ) -> None:
        self.lock = threading.RLock()

        if serial_instance is not None:
            self.ser = serial_instance
            return

        if serial is None:  # pragma: no cover - depends on environment
            raise ImportError("pyserial is required. Install it with: pip install pyserial")

        self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout_s)

    @classmethod
    def from_port_config(
        cls,
        config: SerialPortConfig = DEFAULT_PORTS.keysight,
    ) -> "KeysightE3632A":
        """Create a supply from the shared chamber port configuration."""

        return cls(port=config.port, baudrate=config.baudrate, timeout_s=config.timeout_s)

    def close(self) -> None:
        close = getattr(self.ser, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "KeysightE3632A":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def write(self, command: str, delay_s: float = 0.10) -> None:
        """Write a SCPI command using the controller line-ending convention."""

        with self.lock:
            self.ser.write((command + "\n").encode())
            time.sleep(delay_s)

    def query(self, command: str, delay_s: float = 0.10) -> str:
        """Send a SCPI query and return the stripped response."""

        with self.lock:
            reset = getattr(self.ser, "reset_input_buffer", None)
            if callable(reset):
                reset()
            self.ser.write((command + "\n").encode())
            time.sleep(delay_s)
            return self.ser.readline().decode(errors="ignore").strip()

    def query_float(self, command: str, delay_s: float = 0.10) -> Optional[float]:
        """Return a SCPI query response parsed as float, or ``None``."""

        return parse_optional_float(self.query(command, delay_s=delay_s))

    def query_bool(self, command: str, delay_s: float = 0.10) -> Optional[bool]:
        """Return True/False for SCPI queries that answer 1/0."""

        value = self.query_float(command, delay_s=delay_s)
        if value is None:
            return None
        return bool(int(value))

    # ------------------------------------------------------------------
    # Readback helpers matching controller behaviour
    # ------------------------------------------------------------------
    def read_voltage_current(self) -> VoltageCurrentReadback:
        """Read measured voltage and current.

        This follows the simple Sputtering-Annealing pattern: enter remote
        mode, query voltage, query current, and return local mode.
        """

        self.write("system:remote")
        voltage = self.query_float("measure:voltage?")
        current = self.query_float("measure:current?")
        self.write("system:local")
        return VoltageCurrentReadback(voltage_v=voltage, current_a=current)

    def protection_status(self) -> ProtectionStatus:
        """Read output/OCP/OVP status without changing output state."""

        return ProtectionStatus(
            output_on=self.query_bool("OUTP?"),
            ocp_tripped=self.query_bool("CURR:PROT:TRIP?"),
            ovp_tripped=self.query_bool("VOLT:PROT:TRIP?"),
        )

    # ------------------------------------------------------------------
    # Command helpers used by the packaged workflows
    # ------------------------------------------------------------------
    def set_remote(self) -> None:
        self.write("SYST:REM")

    def set_local(self) -> None:
        self.write("SYST:LOC")

    def clear_status(self) -> None:
        self.write("*CLS")

    def set_range(self, range_name: str) -> None:
        self.write(f"VOLT:RANG {range_name}")

    def output_on(self) -> None:
        self.write("OUTP ON")

    def output_off(self) -> None:
        self.write("OUTP OFF")

    def clear_voltage_protection(self) -> None:
        self.write("VOLT:PROT:CLE")

    def clear_current_protection(self) -> None:
        self.write("CURR:PROT:CLE")

    def set_voltage_limit(self, voltage_v: float, *, max_voltage_v: float | None = None) -> float:
        """Set normal voltage compliance limit and return the commanded value."""

        value = float(voltage_v)
        if max_voltage_v is not None:
            value = max(0.0, min(value, float(max_voltage_v)))
        self.write(f"VOLT {value:.3f}")
        return value

    def set_current(self, current_a: float, *, max_current_a: float | None = None) -> float:
        """Set current and return the commanded value.

        Passing ``max_current_a`` applies the established soft-cap behaviour while
        keeping the cap visible at the call site.
        """

        value = float(current_a)
        if max_current_a is not None:
            value = max(0.0, min(value, float(max_current_a)))
        self.write(f"CURR {value:.3f}")
        return value

    def configure_protection(
        self,
        *,
        ovp_v: float,
        ocp_a: float,
        enable_ovp: bool = True,
        enable_ocp: bool = True,
        clear_latches: bool = True,
    ) -> None:
        """Configure OVP/OCP thresholds using explicit caller-provided values."""

        self.write(f"VOLT:PROT {float(ovp_v):.3f}")
        if enable_ovp:
            self.write("VOLT:PROT:STAT ON")
        self.write(f"CURR:PROT {float(ocp_a):.3f}")
        if enable_ocp:
            self.write("CURR:PROT:STAT ON")
        if clear_latches:
            self.clear_voltage_protection()
            self.clear_current_protection()

    def force_zero_output(self) -> None:
        """Set current to 0 A and switch output off for a safe shutdown."""

        self.set_current(0.0)
        self.output_off()
