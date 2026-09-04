"""Default serial-port configuration for the NPG synthesis chamber.

Keep all COM ports in one place so workflows do not duplicate the same
`device_info` dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SerialPortConfig:
    """Configuration for one serial device."""

    port: str
    baudrate: int
    timeout_s: float = 1.0


@dataclass(frozen=True)
class ChamberPorts:
    """Default ports currently used by the synthesis chamber scripts."""

    ck1_qmb: SerialPortConfig = SerialPortConfig("COM4", 115200)
    sample_qmb: SerialPortConfig = SerialPortConfig("COM16", 115200)
    xgs600: SerialPortConfig = SerialPortConfig("COM6", 9600)
    oven_pid: SerialPortConfig = SerialPortConfig("COM9", 9600)
    keysight: SerialPortConfig = SerialPortConfig("COM17", 9600)
    ck1_arduino: SerialPortConfig = SerialPortConfig("COM3", 9600)
    pyrometer: SerialPortConfig = SerialPortConfig("COM10", 38400)


DEFAULT_PORTS = ChamberPorts()
