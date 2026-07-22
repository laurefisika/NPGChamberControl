"""Arduino CK-1 crucible temperature reader."""

from __future__ import annotations

import re
from typing import Optional

try:  # pragma: no cover - depends on runtime computer
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

from npg_chamber.config.ports import DEFAULT_PORTS, SerialPortConfig


class ArduinoTemperatureError(RuntimeError):
    """Base exception for Arduino temperature read errors."""


def parse_temperature_c(text: str) -> Optional[float]:
    """Parse a temperature value from a permissive Arduino text line."""

    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", text or "")
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


class ArduinoTemperatureReader:
    """Read CK-1 crucible temperature from the Arduino serial line."""

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
        config: SerialPortConfig = DEFAULT_PORTS.ck1_arduino,
    ) -> "ArduinoTemperatureReader":
        return cls(port=config.port, baudrate=config.baudrate, timeout_s=config.timeout_s)

    def close(self) -> None:
        close = getattr(self.ser, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "ArduinoTemperatureReader":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def read_line(self) -> str:
        return self.ser.readline().decode(errors="ignore").strip()

    def read_temperature_c(self) -> Optional[float]:
        return parse_temperature_c(self.read_line())
