"""IMPAC IPE 140 read-only monitoring and controlled emissivity setup.

The chamber uses the pyrometer as a monitoring device only.  Temperature
readings never participate in PID, Keysight, shutter, phase-transition, or
safety decisions.  The only writable setting exposed to the operator is the
instrument emissivity, which is written once at phase startup and immediately
read back for verification.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:  # pragma: no cover - exercised with pyserial on the chamber PC
    import serial
except ImportError:  # pragma: no cover - keeps profile/protocol tests importable
    serial = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PyrometerSerialConfig:
    port: str = "COM10"
    baudrate: int = 38400
    address: str = "00"
    timeout_s: float = 0.7


@dataclass(frozen=True)
class PyrometerProfile:
    enabled: bool = True
    profile_name: str = "Au/mica — validated"
    emissivity_percent: float = 10.0
    sample_slope: float = 1.69959
    sample_intercept_c: float = 28.20193
    minimum_valid_pyrometer_c: float = 90.0
    write_emissivity_at_start: bool = True
    default_view: str = "oven"

    def estimated_sample_c(self, pyrometer_c: float) -> float:
        """Return calibrated sample temperature or NaN below the valid range."""

        value = float(pyrometer_c)
        if not math.isfinite(value) or value < self.minimum_valid_pyrometer_c:
            return float("nan")
        return self.sample_slope * value + self.sample_intercept_c


class ImpacIPE140:
    """Small UPP client for one IPE 140 on an RS-232 virtual COM port."""

    def __init__(self, config: PyrometerSerialConfig = PyrometerSerialConfig()) -> None:
        self.config = config
        self._lock = threading.RLock()
        self.ser: Optional[serial.Serial] = None

    @property
    def is_open(self) -> bool:
        return bool(self.ser is not None and self.ser.is_open)

    def open(self) -> None:
        if self.is_open:
            return
        if serial is None:
            raise RuntimeError("pyserial is required to open the IMPAC pyrometer")
        ser = serial.Serial(
            port=self.config.port,
            baudrate=self.config.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.config.timeout_s,
            write_timeout=self.config.timeout_s,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass
        self.ser = ser

    def _exchange(self, command: str, *, wait_s: float = 0.04) -> bytes:
        with self._lock:
            if not self.is_open or self.ser is None:
                raise RuntimeError("Pyrometer serial port is not open")
            self.ser.reset_input_buffer()
            self.ser.write((command + "\r").encode("ascii"))
            self.ser.flush()
            if wait_s > 0:
                time.sleep(wait_s)
            raw = self.ser.read_until(b"\r")
            return raw.rstrip(b"\r\n")

    def read_temperature_c(self) -> float:
        raw = self._exchange(f"{self.config.address}ms")
        if not raw:
            raise TimeoutError("No temperature reply from pyrometer")
        text = raw.decode("ascii", errors="replace").strip()
        if text == "77770":
            raise RuntimeError("Pyrometer internal temperature is too high (77770)")
        if text == "88880":
            raise RuntimeError("Pyrometer measurement overflow (88880)")
        if not text.isdigit():
            raise ValueError(f"Unexpected pyrometer temperature reply: {raw!r}")
        return int(text) / 10.0

    @staticmethod
    def _format_emissivity(percent: float) -> str:
        value = float(percent)
        if not math.isfinite(value) or value < 10.0 or value > 100.0:
            raise ValueError("Pyrometer emissivity must be between 10.0% and 100.0%")
        # UPP encodes 0.1% steps: 10.0% -> 0100, 97.0% -> 0970.
        return f"{int(round(value * 10.0)):04d}"

    @staticmethod
    def _parse_emissivity(raw: bytes) -> float:
        if not raw:
            raise TimeoutError("No emissivity reply from pyrometer")
        text = raw.decode("ascii", errors="replace").strip()
        if not text.isdigit():
            raise ValueError(f"Unexpected pyrometer emissivity reply: {raw!r}")
        return int(text) / 10.0

    def read_emissivity_percent(self) -> float:
        return self._parse_emissivity(self._exchange(f"{self.config.address}em"))

    def set_emissivity_percent(self, percent: float, *, verify: bool = True) -> float:
        encoded = self._format_emissivity(percent)
        reply = self._exchange(f"{self.config.address}em{encoded}")
        text = reply.decode("ascii", errors="replace").strip().lower()
        if text not in {"ok", encoded.lower()}:
            raise RuntimeError(f"Unexpected emissivity-write reply: {reply!r}")
        if not verify:
            return float(percent)
        confirmed = self.read_emissivity_percent()
        if abs(confirmed - float(percent)) > 0.11:
            raise RuntimeError(
                f"Pyrometer emissivity verification failed: requested {percent:.1f}%, "
                f"read back {confirmed:.1f}%"
            )
        return confirmed

    def ensure_emissivity_percent(self, percent: float) -> tuple[float, bool]:
        """Verify emissivity and write only when the instrument differs.

        Returns ``(confirmed_percent, changed)``.  Avoiding an unnecessary write
        is useful when Phase 03 follows Phase 01 with the same material profile.
        """

        requested = float(percent)
        current = self.read_emissivity_percent()
        if abs(current - requested) <= 0.11:
            return current, False
        confirmed = self.set_emissivity_percent(requested, verify=True)
        return confirmed, True

    def reset_buffers(self) -> None:
        if not self.is_open or self.ser is None:
            return
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        try:
            self.ser.reset_output_buffer()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            if self.ser is None:
                return
            try:
                self.reset_buffers()
            finally:
                try:
                    self.ser.close()
                finally:
                    self.ser = None
