#!/usr/bin/env python3
from __future__ import annotations

import math
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError:
    serial = None


class TestError(RuntimeError):
    pass


class PressureSafetyError(TestError):
    pass


@dataclass
class Status:
    mode: str
    interlock: str
    details: str
    raw: str


class ReportLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.pressures: list[float] = []

    def add(self, message: str = "") -> None:
        if message:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            line = f"[{stamp}] {message}"
        else:
            line = ""
        self.lines.append(line)
        print(line)

    def add_pressure(self, pressure_mbar: float, context: str) -> None:
        self.pressures.append(pressure_mbar)
        self.add(f"PRESSURE [{context}]: {pressure_mbar:.6e} mbar")

    def save(
        self,
        report_dir: Path,
        *,
        filename_prefix: str,
        title: str,
        result: str,
        reason: str,
        metadata: dict[str, object],
        permitted_commands: list[str],
    ) -> Path:
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = report_dir / f"{filename_prefix}_{stamp}.txt"

        p_min = min(self.pressures) if self.pressures else None
        p_max = max(self.pressures) if self.pressures else None

        header = [
            title,
            "=" * len(title),
            f"Timestamp: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"Result: {result}",
            f"Reason: {reason}",
        ]
        for key, value in metadata.items():
            header.append(f"{key}: {value}")
        if p_min is not None:
            header.append(f"Minimum recorded pressure: {p_min:.6e} mbar")
            header.append(f"Maximum recorded pressure: {p_max:.6e} mbar")
        header.extend(["", "Permitted COSCON commands/patterns:"])
        header.extend(f"  - {item}" for item in permitted_commands)
        header.extend(["", "LOG", "---"])

        path.write_text("\n".join(header + self.lines) + "\n", encoding="utf-8")
        return path


class CosconUDP:
    def __init__(
        self,
        ip: str,
        port: int,
        timeout_s: float,
        logger: ReportLogger,
        validator,
    ) -> None:
        self.ip = ip
        self.port = port
        self.timeout_s = timeout_s
        self.logger = logger
        self.validator = validator

    def send(self, command: str) -> str:
        if not self.validator(command):
            raise TestError(f"Blocked COSCON command: {command!r}")

        self.logger.add(f"-> {command}")
        payload = (command + "\r").encode("ascii")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendto(payload, (self.ip, self.port))
            try:
                data, sender = sock.recvfrom(4096)
            except socket.timeout as exc:
                raise TestError(
                    f"No COSCON reply to {command!r} within {self.timeout_s:.1f} s."
                ) from exc

        if sender[0] != self.ip:
            raise TestError(
                f"Unexpected reply source for {command!r}: {sender[0]}:{sender[1]}."
            )

        reply = data.decode("ascii", errors="replace").strip("\x00\r\n ")
        self.logger.add(f"<- {reply}")

        if not reply:
            raise TestError(f"Empty reply to {command!r}.")
        if "ERROR" in reply.upper() or "FAIL" in reply.upper():
            raise TestError(f"COSCON rejected {command!r}: {reply}")
        return reply


MODE_RE = re.compile(r"\bMode=(?P<mode>[^\s]+)", re.IGNORECASE)
INTERLOCK_RE = re.compile(r"\bInterlock=(?P<interlock>[^\s]+)", re.IGNORECASE)
DETAILS_RE = re.compile(r'Details="(?P<details>.*)"', re.IGNORECASE)


def parse_status(reply: str) -> Status:
    mode_match = MODE_RE.search(reply)
    interlock_match = INTERLOCK_RE.search(reply)
    details_match = DETAILS_RE.search(reply)

    if not mode_match:
        raise TestError(f"Could not parse Mode from: {reply!r}")
    if not interlock_match:
        raise TestError(f"Could not verify Interlock from: {reply!r}")

    return Status(
        mode=mode_match.group("mode"),
        interlock=interlock_match.group("interlock"),
        details=details_match.group("details") if details_match else "",
        raw=reply,
    )


def get_status(client: CosconUDP) -> Status:
    return parse_status(client.send("GetStatus"))


def require_interlock_ok(status: Status, context: str) -> None:
    if status.interlock.lower() != "ok":
        raise TestError(
            f"Interlock is not OK {context}: {status.interlock} ({status.details})"
        )


def wait_for_modes(
    client: CosconUDP,
    target_modes: set[str],
    timeout_s: float,
    poll_s: float,
    *,
    require_ok_interlock: bool,
    allowed_transient_modes: Optional[set[str]] = None,
) -> Status:
    targets = {mode.lower() for mode in target_modes}
    allowed = {mode.lower() for mode in (allowed_transient_modes or set())}
    deadline = time.monotonic() + timeout_s
    last: Optional[Status] = None

    while time.monotonic() < deadline:
        status = get_status(client)
        last = status

        if require_ok_interlock:
            require_interlock_ok(status, f"while waiting for {sorted(target_modes)}")

        mode = status.mode.lower()
        if mode in targets:
            return status
        if mode == "error":
            raise TestError(f"COSCON entered Error mode: {status.details}")
        if allowed and mode not in allowed:
            raise TestError(
                f"Unexpected mode while waiting for {sorted(target_modes)}: "
                f"{status.mode} ({status.details})"
            )
        time.sleep(poll_s)

    raise TestError(
        f"Timeout waiting for Mode in {sorted(target_modes)}. "
        f"Last status: {last.raw if last else 'no status received'}"
    )


class XGS600:
    COMMAND = b"#0002USYNTH\r"
    NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

    def __init__(
        self,
        port: str,
        baud: int,
        timeout_s: float,
        logger: ReportLogger,
    ) -> None:
        if serial is None:
            raise TestError(
                "pyserial is required for the active test. "
                "Run it from the project .venv or install pyserial."
            )

        self.logger = logger
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=timeout_s,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as exc:
            raise TestError(
                f"Could not open XGS600 pressure gauge on {port}: {exc}. "
                "Close Phase 2 and any other program using this COM port."
            ) from exc
        self.timeout_s = timeout_s

    def close(self) -> None:
        try:
            if self.ser.is_open:
                try:
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
                except Exception:
                    pass
                self.ser.close()
        except Exception as exc:
            self.logger.add(f"Pressure-port cleanup warning: {exc}")

    def read_mbar(self) -> float:
        try:
            self.ser.reset_input_buffer()
            self.ser.write(self.COMMAND)
            self.ser.flush()
            time.sleep(0.12)

            deadline = time.monotonic() + self.timeout_s
            buffer = bytearray()
            last_data_at: Optional[float] = None

            while time.monotonic() < deadline:
                waiting = self.ser.in_waiting
                if waiting:
                    buffer.extend(self.ser.read(waiting))
                    last_data_at = time.monotonic()
                elif buffer and last_data_at is not None and time.monotonic() - last_data_at >= 0.08:
                    break
                time.sleep(0.02)

            if not buffer:
                buffer.extend(self.ser.read(100))
            text = bytes(buffer).decode("ascii", errors="ignore").strip()
        except Exception as exc:
            raise PressureSafetyError(f"XGS600 communication failed: {exc}") from exc

        cleaned = text.lstrip(">").strip()
        if cleaned.lower() in {"nan", "+nan", "-nan", ""}:
            raise PressureSafetyError(
                f"Pressure monitoring unavailable: XGS600 returned {text!r}."
            )

        match = self.NUMBER_RE.search(cleaned)
        if not match:
            raise PressureSafetyError(
                f"Could not parse pressure from XGS600 reply {text!r}."
            )

        value = float(match.group(0))
        if not math.isfinite(value) or value <= 0:
            raise PressureSafetyError(f"Unsafe pressure value: {value!r}.")
        return value


def read_pressure_in_window(
    gauge: XGS600,
    logger: ReportLogger,
    *,
    min_mbar: float,
    max_mbar: float,
    context: str,
) -> float:
    pressure = gauge.read_mbar()
    logger.add_pressure(pressure, context)
    if pressure < min_mbar or pressure > max_mbar:
        raise PressureSafetyError(
            f"Pressure {pressure:.6e} mbar is outside the allowed "
            f"window [{min_mbar:.6e}, {max_mbar:.6e}] mbar."
        )
    return pressure


def safe_stop_operating_source(
    client: CosconUDP,
    logger: ReportLogger,
    *,
    standby_timeout_s: float,
    off_timeout_s: float,
    poll_s: float,
) -> bool:
    """
    Best-effort sequence for an active source:
      Operating/SwitchingToOperate -> Standby -> Off.
    No state-changing command is blindly retried.
    """
    try:
        status = get_status(client)
    except Exception as exc:
        logger.add(f"SAFE STOP: initial status query failed: {exc}")
        status = None

    if status is not None and status.mode.lower() == "off":
        logger.add("SAFE STOP: COSCON already reports Mode=Off.")
        return True

    if status is not None and status.mode.lower() == "degassing":
        logger.add("SAFE STOP: device is Degassing; requesting Off directly.")
    else:
        logger.add("SAFE STOP: requesting Standby.")
        try:
            client.send("SwitchToStandby")
            status = wait_for_modes(
                client,
                {"Standby", "Off"},
                standby_timeout_s,
                poll_s,
                require_ok_interlock=False,
                allowed_transient_modes={
                    "operating",
                    "switchingtooperate",
                    "switchingtostandby",
                    "standby",
                    "off",
                    "error",
                },
            )
            logger.add(
                f"SAFE STOP: reached Mode={status.mode}, "
                f"Interlock={status.interlock}, Details={status.details!r}"
            )
            if status.mode.lower() == "off":
                return True
        except Exception as exc:
            logger.add(f"SAFE STOP warning: Standby could not be confirmed: {exc}")

    logger.add("SAFE STOP: requesting Off.")
    try:
        client.send("SwitchToOff")
        status = wait_for_modes(
            client,
            {"Off"},
            off_timeout_s,
            poll_s,
            require_ok_interlock=False,
            allowed_transient_modes={
                "operating",
                "switchingtooperate",
                "switchingtostandby",
                "standby",
                "switchingtooff",
                "off",
                "error",
                "degassing",
            },
        )
        logger.add(
            f"SAFE STOP: final Off confirmed, Interlock={status.interlock}, "
            f"Details={status.details!r}"
        )
        return True
    except Exception as exc:
        logger.add(
            "CRITICAL: automatic return to Off could not be confirmed: "
            f"{exc}. Use the local COSCON controls immediately."
        )
        return False
