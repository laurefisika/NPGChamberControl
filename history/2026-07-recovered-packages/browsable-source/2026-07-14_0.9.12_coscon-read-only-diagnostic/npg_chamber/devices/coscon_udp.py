"""Read-only UDP client for the SPECS COSCON IS.

The COSCON IS manual documents a plain-text UDP interface on port 2005.
Commands and replies are ASCII strings terminated by a carriage return.

This module deliberately exposes only read-only operations. State-changing
commands such as ``SwitchToStandby``, ``SwitchToOff``, ``SwitchToOperate``,
``SwitchToDegas``, ``Reset``, ``SetNetwork``, ``StorePreset`` and
``DeletePreset`` are rejected by the public query method.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from typing import Callable, Mapping

DEFAULT_COSCON_HOST = "192.168.236.186"
DEFAULT_COSCON_PORT = 2005
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_RETRIES = 1

READ_ONLY_COMMANDS = frozenset(
    {
        "Info",
        "Uptime",
        "GetDeviceType",
        "GetFirmwareVersion",
        "GetDeviceSerial",
        "GetNetwork",
        "GetIdentify",
        "GetStatus",
        "GetMonitorValues",
        "GetDiagnosticValues",
        "GetTargetValues",
        "ReadNumberOfPresets",
        "ReadPreset",
    }
)

_FIELD_PATTERN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)=(?:\"([^\"]*)\"|([^\s\r\n]+))"
)


class CosconUDPError(RuntimeError):
    """Base exception for COSCON UDP communication failures."""


class CosconUDPTimeout(CosconUDPError):
    """Raised when the COSCON does not reply within the configured timeout."""


class CosconReadOnlyViolation(CosconUDPError):
    """Raised when a state-changing or malformed command is requested."""


@dataclass(frozen=True)
class CosconReply:
    """One COSCON request/reply exchange."""

    command: str
    raw: str
    ok: bool
    fields: Mapping[str, str]
    source_host: str
    source_port: int
    elapsed_s: float


def parse_coscon_fields(response: str) -> dict[str, str]:
    """Extract ``name=value`` fields from a COSCON reply.

    Quoted values such as ``Details="Device is Off"`` are preserved without
    the quotation marks. The raw response remains available on ``CosconReply``
    for commands whose output is not represented as key/value pairs.
    """

    fields: dict[str, str] = {}
    for match in _FIELD_PATTERN.finditer(response or ""):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        fields[match.group(1)] = value
    return fields


def first_integer(response: str) -> int | None:
    """Return the first integer appearing after ``OK`` in a reply, if any."""

    text = response or ""
    ok_index = text.upper().find("OK")
    if ok_index >= 0:
        text = text[ok_index + 2 :]
    match = re.search(r"[-+]?\d+", text)
    return int(match.group(0)) if match else None


class CosconReadOnlyClient:
    """Strictly read-only COSCON IS UDP client.

    ``socket_factory`` is injectable so communication can be regression-tested
    without chamber hardware.
    """

    def __init__(
        self,
        host: str = DEFAULT_COSCON_HOST,
        port: int = DEFAULT_COSCON_PORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        *,
        socket_factory: Callable[[int, int], object] = socket.socket,
    ) -> None:
        host = host.strip()
        if not host:
            raise ValueError("COSCON host must not be empty.")
        if not 1 <= int(port) <= 65535:
            raise ValueError("COSCON UDP port must be between 1 and 65535.")
        if timeout_s <= 0:
            raise ValueError("COSCON timeout must be positive.")
        if retries < 0:
            raise ValueError("COSCON retries must be zero or greater.")

        self.host = host
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self._socket_factory = socket_factory

    @staticmethod
    def _validate_read_only_command(command: str) -> str:
        normalized = " ".join((command or "").strip().split())
        if not normalized:
            raise CosconReadOnlyViolation("Empty COSCON command rejected.")

        parts = normalized.split(" ")
        base = parts[0]
        if base not in READ_ONLY_COMMANDS:
            raise CosconReadOnlyViolation(
                f"Command {base!r} is not in the read-only COSCON whitelist."
            )

        if base == "ReadPreset":
            if len(parts) != 2 or not parts[1].isdigit():
                raise CosconReadOnlyViolation(
                    "ReadPreset must be followed by one non-negative integer index."
                )
        elif len(parts) != 1:
            raise CosconReadOnlyViolation(
                f"Read-only command {base!r} does not accept parameters."
            )

        return normalized

    @staticmethod
    def _reply_is_ok(response: str) -> bool:
        upper = (response or "").upper()
        if not upper.strip():
            return False
        return not any(marker in upper for marker in (" ERROR", "ERROR:", " FAIL", "FAILED"))

    def query(self, command: str) -> CosconReply:
        """Send one whitelisted read-only command and return its reply."""

        normalized = self._validate_read_only_command(command)
        payload = (normalized + "\r").encode("ascii", errors="strict")
        last_error: BaseException | None = None

        for attempt in range(self.retries + 1):
            started = time.monotonic()
            sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                settimeout = getattr(sock, "settimeout")
                settimeout(self.timeout_s)
                getattr(sock, "sendto")(payload, (self.host, self.port))
                data, source = getattr(sock, "recvfrom")(8192)
                elapsed = time.monotonic() - started
                raw = data.decode("ascii", errors="replace").strip("\x00\r\n ")
                source_host, source_port = str(source[0]), int(source[1])
                return CosconReply(
                    command=normalized,
                    raw=raw,
                    ok=self._reply_is_ok(raw),
                    fields=parse_coscon_fields(raw),
                    source_host=source_host,
                    source_port=source_port,
                    elapsed_s=elapsed,
                )
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
            except OSError as exc:
                raise CosconUDPError(
                    f"UDP communication failed for {normalized!r}: {exc}"
                ) from exc
            finally:
                close = getattr(sock, "close", None)
                if callable(close):
                    close()

        raise CosconUDPTimeout(
            f"No reply from COSCON {self.host}:{self.port} for {normalized!r} "
            f"after {self.retries + 1} attempt(s), timeout {self.timeout_s:.2f} s."
        ) from last_error

    def info(self) -> CosconReply:
        return self.query("Info")

    def uptime(self) -> CosconReply:
        return self.query("Uptime")

    def get_device_type(self) -> CosconReply:
        return self.query("GetDeviceType")

    def get_firmware_version(self) -> CosconReply:
        return self.query("GetFirmwareVersion")

    def get_device_serial(self) -> CosconReply:
        return self.query("GetDeviceSerial")

    def get_network(self) -> CosconReply:
        return self.query("GetNetwork")

    def get_identify(self) -> CosconReply:
        return self.query("GetIdentify")

    def get_status(self) -> CosconReply:
        return self.query("GetStatus")

    def get_monitor_values(self) -> CosconReply:
        return self.query("GetMonitorValues")

    def get_diagnostic_values(self) -> CosconReply:
        return self.query("GetDiagnosticValues")

    def get_target_values(self) -> CosconReply:
        return self.query("GetTargetValues")

    def read_number_of_presets(self) -> CosconReply:
        return self.query("ReadNumberOfPresets")

    def read_preset(self, index: int) -> CosconReply:
        if isinstance(index, bool) or int(index) != index or index < 0:
            raise ValueError("Preset index must be a non-negative integer.")
        return self.query(f"ReadPreset {int(index)}")
