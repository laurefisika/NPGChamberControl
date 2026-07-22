"""Quartz microbalance protocol helpers.

The chamber scripts use the same three QMB commands repeatedly:
``thickness``, ``rate`` and ``zero``. This module centralises the byte-frame
construction and the simple response parsing used in the legacy scripts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

try:  # pragma: no cover - depends on runtime computer
    import serial
except ImportError:  # pragma: no cover
    serial = None  # type: ignore

from npg_chamber.config.ports import SerialPortConfig

STX = b"\x02"
ADDR = b"\x10"
CMD_RSP = b"\x80"
CR = b"\x0D"

SUBCOMMANDS = {
    "thickness": b"S",
    "rate": b"T",
    "zero": b"B",
}


class QMBError(RuntimeError):
    """Base exception for QMB errors."""


def calculate_checksum(command: bytes) -> bytes:
    """Calculate the two-byte ASCII checksum used by the QMB controller."""

    checksum = sum(command) % 256
    upper_nibble = (checksum >> 4) & 0x0F
    lower_nibble = checksum & 0x0F
    return bytes([upper_nibble + 0x30, lower_nibble + 0x30])


def build_command(kind: str) -> bytes:
    """Build a QMB command for ``thickness``, ``rate`` or ``zero``."""

    if kind not in SUBCOMMANDS:
        raise ValueError(f"Unknown QMB command kind: {kind!r}")
    payload = ADDR + CMD_RSP + SUBCOMMANDS[kind]
    return STX + payload + calculate_checksum(payload) + CR


COMMANDS = {kind: build_command(kind) for kind in SUBCOMMANDS}


def parse_numeric_response(raw: bytes) -> Optional[float]:
    """Parse a numeric QMB response using the legacy crop ``response[3:-3]``.

    The legacy scripts crop the first three and last three bytes before calling
    ``float(...)``. This function keeps that behaviour exactly for compatibility.
    """

    if not raw:
        return None
    try:
        cropped = raw[3:-3].decode(errors="ignore").strip()
        if not cropped:
            return None
        return float(cropped)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class QMBReadback:
    thickness_a: Optional[float] = None
    rate_a_per_s: Optional[float] = None


class QMBController:
    """Serial wrapper for one QMB channel."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
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
    def from_port_config(cls, config: SerialPortConfig) -> "QMBController":
        return cls(port=config.port, baudrate=config.baudrate, timeout_s=config.timeout_s)

    def close(self) -> None:
        close = getattr(self.ser, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "QMBController":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def send(self, kind: str, delay_s: float = 0.10) -> bytes:
        self.ser.write(build_command(kind))
        time.sleep(delay_s)
        in_waiting = getattr(self.ser, "in_waiting", 0) or 64
        return self.ser.read(in_waiting)

    def zero(self, delay_s: float = 0.10) -> None:
        self.ser.write(COMMANDS["zero"])
        time.sleep(delay_s)

    def read_thickness_a(self, delay_s: float = 0.10) -> Optional[float]:
        return parse_numeric_response(self.send("thickness", delay_s=delay_s))

    def read_rate_a_per_s(self, delay_s: float = 0.10) -> Optional[float]:
        return parse_numeric_response(self.send("rate", delay_s=delay_s))

    def read_both(self, delay_s: float = 0.10) -> QMBReadback:
        return QMBReadback(
            thickness_a=self.read_thickness_a(delay_s=delay_s),
            rate_a_per_s=self.read_rate_a_per_s(delay_s=delay_s),
        )
