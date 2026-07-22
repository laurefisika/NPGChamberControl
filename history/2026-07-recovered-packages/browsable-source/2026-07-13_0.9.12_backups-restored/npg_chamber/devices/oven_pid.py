"""RKC-style oven PID serial controller.

This module centralizes the PID communication code that was duplicated across
several legacy scripts. It keeps the same protocol used in the working scripts:

- read identifiers with: ``EOT + address + identifier + ENQ``
- write setpoint S1 with: ``EOT + address + STX + identifier + data + ETX + BCC``
- BCC is the XOR of all bytes from IDENTIFIER to ETX inclusive.

The class is intentionally small and conservative. It does not change any
experimental logic by itself; workflows decide when it is safe to write S1.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

try:  # pragma: no cover - exercised only when pyserial is installed
    import serial
except ImportError:  # pragma: no cover - keeps pure protocol tests importable
    serial = None  # type: ignore[assignment]

from npg_chamber.config.ports import DEFAULT_PORTS, SerialPortConfig


EOT = b"\x04"
ENQ = b"\x05"
STX = b"\x02"
ETX = b"\x03"
ACK = b"\x06"
NAK = b"\x15"


class OvenPIDError(RuntimeError):
    """Base exception for oven PID communication errors."""


class OvenPIDProtocolError(OvenPIDError):
    """Raised when the PID returns an unexpected protocol response."""


class OvenPIDSafetyError(OvenPIDError):
    """Raised when a requested setpoint write violates a safety guard."""


@dataclass(frozen=True)
class PIDFrame:
    """Parsed response frame from the controller."""

    status: str
    raw: bytes
    decoded: str = ""
    identifier: Optional[str] = None
    data: Optional[str] = None

    @property
    def numeric_value(self) -> Optional[float]:
        """Return ``data`` parsed as float, if possible."""

        if self.data is None:
            return None
        return parse_numeric_ascii(self.data)


def xor_bcc(identifier_plus_data_plus_etx: bytes) -> bytes:
    """Calculate the RKC BCC byte as XOR from identifier through ETX."""

    value = 0
    for byte in identifier_plus_data_plus_etx:
        value ^= byte
    return bytes([value])


def parse_frame(raw: bytes) -> PIDFrame:
    """Parse a raw PID response.

    Known single-byte responses are mapped to statuses: ``ACK``, ``NAK`` and
    ``EOT``. Data frames are expected as ``STX + IDENT(2) + DATA + ETX + BCC``.
    The BCC byte is not currently validated because the legacy scripts did not
    validate it either; the parser keeps the same behaviour for compatibility.
    """

    if raw == b"":
        return PIDFrame(status="NO_RESPONSE", raw=raw)
    if raw == ACK:
        return PIDFrame(status="ACK", raw=raw)
    if raw == NAK:
        return PIDFrame(status="NAK", raw=raw)
    if raw == EOT:
        return PIDFrame(status="EOT", raw=raw)

    decoded = raw.decode(errors="ignore")
    if len(raw) >= 5 and raw[0:1] == STX:
        try:
            etx_index = raw.index(ETX)
        except ValueError:
            return PIDFrame(status="UNKNOWN_FRAME", raw=raw, decoded=decoded)

        core = raw[1:etx_index]  # IDENTIFIER + DATA
        if len(core) < 2:
            return PIDFrame(status="SHORT_FRAME", raw=raw, decoded=decoded)

        identifier = core[:2].decode(errors="ignore")
        data = core[2:].decode(errors="ignore")
        return PIDFrame(
            status="DATA",
            raw=raw,
            decoded=decoded,
            identifier=identifier,
            data=data,
        )

    return PIDFrame(status="UNKNOWN", raw=raw, decoded=decoded)


def parse_numeric_ascii(data_text: str) -> Optional[float]:
    """Parse the numeric ASCII payload returned by the PID.

    The controller often returns zero-padded numbers such as ``000200``. This
    helper also tolerates small amounts of surrounding non-numeric text.
    """

    stripped = data_text.strip()
    if stripped == "":
        return None

    try:
        return float(stripped)
    except ValueError:
        pass

    allowed = "".join(ch for ch in stripped if ch.isdigit() or ch in ".-")
    if allowed in {"", "-", ".", "-."}:
        return None

    try:
        return float(allowed)
    except ValueError:
        return None


def format_target_like_current_data(current_data: str, target_value: float) -> str:
    """Format a new setpoint using the same shape as the current S1 payload.

    Examples
    --------
    ``current_data='000200', target_value=35`` returns ``'000035'``.
    ``current_data='0200.0', target_value=35.5`` returns ``'0035.5'``.
    """

    template = current_data.strip()
    if not template:
        raise ValueError("No current PID setpoint template is available.")

    body = template[1:] if template.startswith("-") else template

    if "." in body:
        left, right = body.split(".", 1)
        decimals = len(right)
        width_left = len(left)
        total_width = width_left + 1 + decimals
        return f"{round(target_value, decimals):0{total_width}.{decimals}f}"

    width = len(body)
    if not float(target_value).is_integer():
        raise ValueError(
            f"The PID returned S1 without decimals ({template!r}); target must be an integer."
        )

    integer_value = int(round(target_value))
    sign = "-" if integer_value < 0 else ""
    digits = str(abs(integer_value)).zfill(width)
    return sign + digits


def ack_name(raw: bytes) -> str:
    """Human-readable name for a one-byte write response."""

    if raw == ACK:
        return "ACK"
    if raw == NAK:
        return "NAK"
    if raw == EOT:
        return "EOT"
    if raw == b"":
        return "NO_RESPONSE"
    return repr(raw)


class OvenPID:
    """Small serial wrapper for the oven PID.

    Parameters
    ----------
    port:
        Serial port, normally ``COM9`` in the synthesis chamber.
    baudrate:
        Serial baudrate, normally ``9600``.
    address:
        PID address as two ASCII characters, normally ``"00"``.
    timeout_s:
        Serial timeout in seconds.
    serial_instance:
        Optional already-open serial-like object. This is useful for tests and
        keeps the protocol code independent from the physical port.
    """

    def __init__(
        self,
        port: str = DEFAULT_PORTS.oven_pid.port,
        baudrate: int = DEFAULT_PORTS.oven_pid.baudrate,
        address: str = "00",
        timeout_s: float = DEFAULT_PORTS.oven_pid.timeout_s,
        serial_instance: object | None = None,
    ) -> None:
        self.address = address
        self.lock = threading.RLock()

        if serial_instance is not None:
            self.ser = serial_instance
            return

        if serial is None:  # pragma: no cover - depends on environment
            raise ImportError("pyserial is required. Install it with: pip install pyserial")

        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout_s,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )

    @classmethod
    def from_port_config(
        cls,
        config: SerialPortConfig = DEFAULT_PORTS.oven_pid,
        address: str = "00",
    ) -> "OvenPID":
        """Create an ``OvenPID`` from a shared ``SerialPortConfig``."""

        return cls(
            port=config.port,
            baudrate=config.baudrate,
            timeout_s=config.timeout_s,
            address=address,
        )

    def close(self) -> None:
        """Close the serial connection if the underlying object supports it."""

        close = getattr(self.ser, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "OvenPID":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def read_identifier_raw(self, identifier: str, wait_s: float = 0.15) -> bytes:
        """Read a raw identifier response, for example ``M1`` or ``S1``."""

        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(EOT)
            time.sleep(0.05)
            self.ser.write(self.address.encode("ascii") + identifier.encode("ascii") + ENQ)
            time.sleep(wait_s)
            in_waiting = getattr(self.ser, "in_waiting", 0) or 64
            return self.ser.read(in_waiting)

    def read_identifier(self, identifier: str, wait_s: float = 0.15) -> PIDFrame:
        """Read and parse a PID identifier."""

        return parse_frame(self.read_identifier_raw(identifier, wait_s=wait_s))

    def read_value(self, identifier: str, wait_s: float = 0.15) -> Optional[float]:
        """Read an identifier and return its numeric value, if available."""

        frame = self.read_identifier(identifier, wait_s=wait_s)
        if frame.status != "DATA":
            return None
        return frame.numeric_value

    def read_process_value_c(self) -> Optional[float]:
        """Read M1, normally the measured oven process value in ºC."""

        return self.read_value("M1")

    def read_setpoint_c(self) -> Optional[float]:
        """Read S1, normally the active oven setpoint in ºC."""

        return self.read_value("S1")

    def build_write_frame(self, identifier: str, data_text: str) -> bytes:
        """Build a write/selecting frame without sending it."""

        body = identifier.encode("ascii") + data_text.encode("ascii") + ETX
        return EOT + self.address.encode("ascii") + STX + body + xor_bcc(body)

    def write_identifier(self, identifier: str, data_text: str, wait_s: float = 0.20) -> bytes:
        """Write an identifier payload and return the raw ACK/NAK response."""

        frame = self.build_write_frame(identifier, data_text)
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            time.sleep(wait_s)
            return self.ser.read(1)

    def write_s1_raw(self, data_text: str, wait_s: float = 0.20) -> bytes:
        """Write raw S1 payload text and return the raw response."""

        return self.write_identifier("S1", data_text, wait_s=wait_s)

    def set_setpoint_c(
        self,
        target_c: float,
        *,
        verify: bool = True,
        max_delta_from_current_c: Optional[float] = None,
        verification_tolerance_c: float = 0.5,
    ) -> float:
        """Set S1 using the current S1 format and optionally verify the write.

        Safety guard
        ------------
        Pass ``max_delta_from_current_c`` to reject unexpectedly large changes.
        For example, a diagnostic script can use ``max_delta_from_current_c=1``
        so it only allows a small +/- 1 ºC test step.

        Returns
        -------
        float
            The verified setpoint if ``verify=True``; otherwise ``target_c``.
        """

        current_frame = self.read_identifier("S1")
        if current_frame.status != "DATA" or current_frame.data is None:
            raise OvenPIDProtocolError(
                f"Could not read current S1 before writing. Status: {current_frame.status}"
            )

        current_value = current_frame.numeric_value
        if current_value is None:
            raise OvenPIDProtocolError(f"Could not parse current S1: {current_frame.data!r}")

        if max_delta_from_current_c is not None:
            delta = abs(float(target_c) - current_value)
            if delta > max_delta_from_current_c:
                raise OvenPIDSafetyError(
                    f"Refusing to change S1 by {delta:.3f} ºC; "
                    f"allowed maximum is {max_delta_from_current_c:.3f} ºC."
                )

        data_text = format_target_like_current_data(current_frame.data, target_c)
        ack = self.write_s1_raw(data_text)
        if ack != ACK:
            raise OvenPIDProtocolError(f"S1 write was not acknowledged. Response: {ack_name(ack)}")

        if not verify:
            return float(target_c)

        confirmed = self.read_setpoint_c()
        if confirmed is None:
            raise OvenPIDProtocolError("Could not verify S1 after writing; no numeric response.")

        if abs(confirmed - float(target_c)) > verification_tolerance_c:
            raise OvenPIDProtocolError(
                f"S1 verification mismatch: requested {target_c:.3f} ºC, "
                f"read back {confirmed:.3f} ºC."
            )

        return confirmed

    def read_pv_sv(self) -> tuple[Optional[float], Optional[float]]:
        """Read process value M1 and setpoint S1."""

        return self.read_process_value_c(), self.read_setpoint_c()
