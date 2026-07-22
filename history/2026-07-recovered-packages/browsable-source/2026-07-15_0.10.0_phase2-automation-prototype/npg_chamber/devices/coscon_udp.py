"""Thread-safe UDP client for the SPECS COSCON IS.

The protocol uses plain ASCII commands terminated by carriage return on UDP
port 2005.  This module intentionally exposes only the commands needed by the
Phase 02 workflow.  Network configuration, preset modification and Reset are
not implemented here.
"""

from __future__ import annotations

import re
import socket
import threading
from dataclasses import dataclass
from typing import Optional


class COSCONError(RuntimeError):
    """Base class for COSCON communication and protocol failures."""


class COSCONCommunicationError(COSCONError):
    """The command/reply exchange could not be completed reliably."""


class COSCONCommandError(COSCONError):
    """The COSCON explicitly rejected a command."""


class COSCONParseError(COSCONError):
    """A reply was received but required fields could not be parsed."""


@dataclass(frozen=True)
class COSCONStatus:
    mode: str
    interlock: str
    details: str
    raw: str

    @property
    def mode_key(self) -> str:
        return self.mode.strip().lower()

    @property
    def interlock_ok(self) -> bool:
        return self.interlock.strip().lower() == "ok"


@dataclass(frozen=True)
class COSCONMonitorValues:
    energy_v: float
    filament_current_a: float
    emission_current_a: float
    raw: str


@dataclass(frozen=True)
class COSCONTargetValues:
    emission_current_a: float
    energy_v: float
    raw: str


@dataclass(frozen=True)
class COSCONDiagnosticValues:
    energy_current_a: float
    filament_voltage_v: float
    anode_voltage_v: float
    repeller_voltage_v: float
    temperature_hv_c: Optional[float]
    temperature_em_c: Optional[float]
    raw: str


_MODE_RE = re.compile(r"\bMode=([^\s]+)", re.IGNORECASE)
_INTERLOCK_RE = re.compile(r"\bInterlock=([^\s]+)", re.IGNORECASE)
_DETAILS_RE = re.compile(r'Details="([^"]*)"', re.IGNORECASE)
_NUMBER_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]*)="
    r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)


def _number_fields(reply: str) -> dict[str, float]:
    fields: dict[str, float] = {}
    for key, raw in _NUMBER_RE.findall(reply):
        try:
            fields[key] = float(raw)
        except ValueError:
            continue
    return fields


def parse_status(reply: str) -> COSCONStatus:
    mode_match = _MODE_RE.search(reply)
    interlock_match = _INTERLOCK_RE.search(reply)
    details_match = _DETAILS_RE.search(reply)
    if not mode_match:
        raise COSCONParseError(f"Could not parse Mode from COSCON reply: {reply!r}")
    if not interlock_match:
        raise COSCONParseError(f"Could not parse Interlock from COSCON reply: {reply!r}")
    return COSCONStatus(
        mode=mode_match.group(1),
        interlock=interlock_match.group(1),
        details=details_match.group(1) if details_match else "",
        raw=reply,
    )


def parse_monitor_values(reply: str) -> COSCONMonitorValues:
    fields = _number_fields(reply)
    required = ("VEnergy", "IFilament", "IEmission")
    missing = [name for name in required if name not in fields]
    if missing:
        raise COSCONParseError(
            f"Missing COSCON monitor fields {missing} in reply: {reply!r}"
        )
    return COSCONMonitorValues(
        energy_v=fields["VEnergy"],
        filament_current_a=fields["IFilament"],
        emission_current_a=fields["IEmission"],
        raw=reply,
    )


def parse_target_values(reply: str) -> COSCONTargetValues:
    fields = _number_fields(reply)
    required = ("Emission", "Energy")
    missing = [name for name in required if name not in fields]
    if missing:
        raise COSCONParseError(
            f"Missing COSCON target fields {missing} in reply: {reply!r}"
        )
    return COSCONTargetValues(
        emission_current_a=fields["Emission"],
        energy_v=fields["Energy"],
        raw=reply,
    )


def parse_diagnostic_values(reply: str) -> COSCONDiagnosticValues:
    fields = _number_fields(reply)
    required = ("IEnergy", "VFilament", "VAnode", "VRepeller")
    missing = [name for name in required if name not in fields]
    if missing:
        raise COSCONParseError(
            f"Missing COSCON diagnostic fields {missing} in reply: {reply!r}"
        )
    return COSCONDiagnosticValues(
        energy_current_a=fields["IEnergy"],
        filament_voltage_v=fields["VFilament"],
        anode_voltage_v=fields["VAnode"],
        repeller_voltage_v=fields["VRepeller"],
        temperature_hv_c=fields.get("TemperatureHV"),
        temperature_em_c=fields.get("TemperatureEM"),
        raw=reply,
    )


class COSCONUDPClient:
    """Small thread-safe client for one COSCON IS controller.

    Every command uses one fresh UDP socket.  A process-local lock serializes
    exchanges from the background monitor and the active Phase 02 controller,
    preventing overlapping query/reply windows.

    State-changing commands are never retried internally.  The caller must
    inspect device state after a timeout because a missing UDP reply does not
    prove that the command was not received.
    """

    READ_ONLY_COMMANDS = frozenset(
        {
            "Info",
            "GetStatus",
            "GetTargetValues",
            "GetMonitorValues",
            "GetDiagnosticValues",
        }
    )
    EXACT_WRITE_COMMANDS = frozenset(
        {
            "SwitchToDegas",
            "SwitchToStandby",
            "SwitchToOff",
        }
    )
    _VALIDATE_RE = re.compile(
        r"^ValidateOperateTarget Emission="
        r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)? "
        r"Energy=[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$"
    )
    _OPERATE_RE = re.compile(
        r"^SwitchToOperate Emission="
        r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)? "
        r"Energy=[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$"
    )

    def __init__(
        self,
        ip: str = "192.168.236.186",
        port: int = 2005,
        timeout_s: float = 2.0,
    ) -> None:
        self.ip = str(ip)
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.lock = threading.RLock()

    @classmethod
    def command_is_allowed(cls, command: str) -> bool:
        return (
            command in cls.READ_ONLY_COMMANDS
            or command in cls.EXACT_WRITE_COMMANDS
            or bool(cls._VALIDATE_RE.fullmatch(command))
            or bool(cls._OPERATE_RE.fullmatch(command))
        )

    def send(self, command: str) -> str:
        if not self.command_is_allowed(command):
            raise COSCONCommandError(f"Blocked unsupported COSCON command: {command!r}")

        payload = (command + "\r").encode("ascii")
        command_name = command.split()[0]

        with self.lock:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(self.timeout_s)
                try:
                    sock.sendto(payload, (self.ip, self.port))
                    data, sender = sock.recvfrom(8192)
                except socket.timeout as exc:
                    raise COSCONCommunicationError(
                        f"No COSCON reply to {command!r} within {self.timeout_s:.1f} s"
                    ) from exc
                except OSError as exc:
                    raise COSCONCommunicationError(
                        f"COSCON UDP exchange failed for {command!r}: {exc}"
                    ) from exc

        if sender[0] != self.ip:
            raise COSCONCommunicationError(
                f"Unexpected COSCON reply source {sender[0]}:{sender[1]}"
            )

        reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
        if not reply:
            raise COSCONCommunicationError(f"Empty COSCON reply to {command!r}")

        # A valid GetStatus response can contain Mode=Error.  Only treat an
        # explicit command-level error prefix as command rejection.
        if re.match(
            rf"^{re.escape(command_name)}\s+ERROR\b",
            reply,
            re.IGNORECASE,
        ) or re.match(r"^ERROR\b", reply, re.IGNORECASE):
            raise COSCONCommandError(f"COSCON rejected {command!r}: {reply}")

        return reply

    def info(self) -> str:
        return self.send("Info")

    def get_status(self) -> COSCONStatus:
        return parse_status(self.send("GetStatus"))

    def get_monitor_values(self) -> COSCONMonitorValues:
        return parse_monitor_values(self.send("GetMonitorValues"))

    def get_target_values(self) -> COSCONTargetValues:
        return parse_target_values(self.send("GetTargetValues"))

    def get_diagnostic_values(self) -> COSCONDiagnosticValues:
        return parse_diagnostic_values(self.send("GetDiagnosticValues"))

    def validate_operate_target(self, emission_a: float, energy_v: float) -> str:
        command = (
            f"ValidateOperateTarget Emission={float(emission_a):.6e} "
            f"Energy={float(energy_v):.6g}"
        )
        return self.send(command)

    def switch_to_operate(self, emission_a: float, energy_v: float) -> str:
        command = (
            f"SwitchToOperate Emission={float(emission_a):.6e} "
            f"Energy={float(energy_v):.6g}"
        )
        return self.send(command)

    def switch_to_degas(self) -> str:
        return self.send("SwitchToDegas")

    def switch_to_standby(self) -> str:
        return self.send("SwitchToStandby")

    def switch_to_off(self) -> str:
        return self.send("SwitchToOff")
