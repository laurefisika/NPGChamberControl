"""IMPAC IPE 140 monitoring and verified emissivity configuration.

The chamber uses the pyrometer for monitoring only. Temperature readings never
participate in PID, Keysight, shutter, phase-transition, or safety decisions.
The only writable instrument setting exposed to the operator is emissivity.

This specific chamber pyrometer reports its current emissivity in the first two
digits of the ``pa`` parameter reply (``00`` represents 100%). The UPP write
command still uses the standard four-digit tenths-of-a-percent field: for
example, 10% -> ``em0100`` and 11% -> ``em0110``. Verification deliberately
uses the proven ``pa`` response because the standalone hardware diagnostic
confirmed that query on this instrument.
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
        """Return the profile equation at every finite pyrometer temperature.

        Values below ``minimum_valid_pyrometer_c`` are extrapolations and must
        be displayed/logged with a warning, but they are intentionally not
        replaced by NaN.
        """

        value = float(pyrometer_c)
        if not math.isfinite(value):
            return float("nan")
        return self.sample_slope * value + self.sample_intercept_c

    def is_within_calibrated_range(self, pyrometer_c: float) -> bool:
        value = float(pyrometer_c)
        return math.isfinite(value) and value >= self.minimum_valid_pyrometer_c

    def calibration_status(self, pyrometer_c: float) -> str:
        if not math.isfinite(float(pyrometer_c)):
            return "unavailable"
        if self.is_within_calibrated_range(pyrometer_c):
            return "OK"
        return (
            "WARNING: extrapolated below calibrated range "
            f"(<{self.minimum_valid_pyrometer_c:.1f} C)"
        )


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
            try:
                self.ser.reset_output_buffer()
            except Exception:
                pass
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

    def read_parameter_string(self) -> str:
        raw = self._exchange(f"{self.config.address}pa")
        if not raw:
            raise TimeoutError("No parameter reply from pyrometer")
        text = raw.decode("ascii", errors="replace").strip()
        if len(text) != 11 or not text.isdigit():
            raise ValueError(f"Unexpected pyrometer parameter reply: {raw!r}")
        return text

    @staticmethod
    def _format_emissivity(percent: float) -> str:
        value = float(percent)
        if not math.isfinite(value) or value < 10.0 or value > 100.0:
            raise ValueError("Pyrometer emissivity must be between 10% and 100%")
        if abs(value - round(value)) > 1e-9:
            raise ValueError(
                "This IPE 140 reports emissivity as a whole percentage; "
                "enter a whole value such as 10, 11 or 35"
            )
        integer = int(round(value))
        # UPP setting commands encode emissivity in tenths of a percent:
        # 10% -> 0100, 11% -> 0110, 100% -> 1000.
        return f"{integer * 10:04d}"

    @staticmethod
    def _parse_emissivity_from_parameter_string(text: str) -> float:
        if len(text) != 11 or not text.isdigit():
            raise ValueError(f"Unexpected pyrometer parameter string: {text!r}")
        digits = text[:2]
        return 100.0 if digits == "00" else float(int(digits))

    def read_emissivity_percent(self) -> float:
        """Read emissivity from the proven ``pa`` parameter response."""

        return self._parse_emissivity_from_parameter_string(self.read_parameter_string())

    def set_emissivity_percent(self, percent: float, *, verify: bool = True) -> float:
        """Write emissivity and verify it independently using ``pa``.

        Some IPE 140 revisions acknowledge a write with ``ok`` and some do not.
        The acknowledgement is therefore informative only; the parameter
        readback is the source of truth.
        """

        encoded = self._format_emissivity(percent)
        reply = self._exchange(f"{self.config.address}em{encoded}", wait_s=0.10)
        if not verify:
            return float(percent)

        time.sleep(0.10)
        confirmed = self.read_emissivity_percent()
        if abs(confirmed - float(percent)) > 0.51:
            raise RuntimeError(
                f"Pyrometer emissivity verification failed: requested {percent:.0f}%, "
                f"read back {confirmed:.0f}% (write reply {reply!r})"
            )
        return confirmed

    def ensure_emissivity_percent(self, percent: float) -> tuple[float, bool]:
        """Verify emissivity and write only when the instrument differs."""

        requested = float(percent)
        current = self.read_emissivity_percent()
        if abs(current - requested) <= 0.51:
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
