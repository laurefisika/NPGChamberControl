"""XGS600 pressure gauge helper."""

from __future__ import annotations

import re
import time
from typing import Optional

try:  # pragma: no cover - depends on runtime computer
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

from npg_chamber.config.ports import DEFAULT_PORTS, SerialPortConfig


class XGS600Error(RuntimeError):
    """Base exception for XGS600 errors."""


def safe_float_from_text(text: str) -> Optional[float]:
    """Extract the first float-like number from an instrument response."""

    matches = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text or "")
    if not matches:
        return None
    try:
        return float(matches[0])
    except ValueError:
        return None


class XGS600Gauge:
    """Small serial wrapper for the chamber XGS600 HFIG pressure reading."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout_s: float = 1.0,
        *,
        serial_instance: object | None = None,
    ) -> None:
        if serial_instance is not None:
            self.ser = serial_instance
            return
        if serial is None:  # pragma: no cover
            raise ImportError("pyserial is required. Install it with: pip install pyserial")
        self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout_s)

    @classmethod
    def from_port_config(
        cls,
        config: SerialPortConfig = DEFAULT_PORTS.xgs600,
    ) -> "XGS600Gauge":
        return cls(port=config.port, baudrate=config.baudrate, timeout_s=config.timeout_s)

    def close(self) -> None:
        close = getattr(self.ser, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "XGS600Gauge":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def read_raw(self, delay_s: float = 0.10) -> str:
        self.ser.write(b"#0002USYNTH\r")
        time.sleep(delay_s)
        in_waiting = getattr(self.ser, "in_waiting", 0) or 100
        return self.ser.read(in_waiting).decode(errors="ignore").strip().lstrip(">")

    def read_pressure_mbar(self, delay_s: float = 0.10) -> float:
        message = self.read_raw(delay_s=delay_s)
        if message.strip().lower() in {"nan", "+nan", "-nan"}:
            return float("nan")
        value = safe_float_from_text(message)
        if value is None:
            raise XGS600Error(f"Could not parse XGS600 pressure from: {message!r}")
        return value
