from __future__ import annotations

from dataclasses import dataclass

import pytest

from npg_chamber.common.serial_handoff import (
    ALL_CHAMBER_PORTS,
    NamedSerialPort,
    SerialHandoffError,
    reset_and_release_port,
    verify_all_chamber_ports_released,
)
from npg_chamber.config.ports import SerialPortConfig


@dataclass
class FakeSerial:
    is_open: bool = True
    port: str | None = "COM_TEST"
    input_reset: int = 0
    output_reset: int = 0
    closes: int = 0

    def open(self) -> None:
        self.is_open = True

    def reset_input_buffer(self) -> None:
        self.input_reset += 1

    def reset_output_buffer(self) -> None:
        self.output_reset += 1

    def close(self) -> None:
        self.closes += 1
        self.is_open = False


def test_all_expected_chamber_ports_are_verified():
    assert [item.config.port for item in ALL_CHAMBER_PORTS] == [
        "COM4",
        "COM16",
        "COM6",
        "COM9",
        "COM17",
        "COM3",
        "COM10",
    ]


def test_reset_and_release_clears_both_buffers_and_closes():
    fake = FakeSerial()
    spec = NamedSerialPort("test", SerialPortConfig("COM_TEST", 9600))

    reset_and_release_port(spec, serial_factory=lambda _spec: fake)

    assert fake.input_reset == 1
    assert fake.output_reset == 1
    assert fake.closes == 1
    assert fake.is_open is False


def test_handoff_retries_a_temporarily_busy_port():
    specs = (
        NamedSerialPort("first", SerialPortConfig("COM1", 9600)),
        NamedSerialPort("second", SerialPortConfig("COM2", 9600)),
    )
    attempts = {"COM1": 0, "COM2": 0}
    opened: list[FakeSerial] = []

    def factory(spec: NamedSerialPort) -> FakeSerial:
        attempts[spec.config.port] += 1
        if spec.config.port == "COM2" and attempts["COM2"] == 1:
            raise PermissionError(13, "Access is denied")
        fake = FakeSerial(port=spec.config.port)
        opened.append(fake)
        return fake

    verified = verify_all_chamber_ports_released(
        context="test",
        ports=specs,
        timeout_s=1.0,
        retry_s=0.01,
        serial_factory=factory,
        sleep=lambda _seconds: None,
    )

    assert verified == specs
    assert attempts == {"COM1": 1, "COM2": 2}
    assert all(item.closes == 1 for item in opened)


def test_handoff_blocks_when_a_port_never_releases():
    spec = NamedSerialPort("busy device", SerialPortConfig("COM9", 9600))

    def factory(_spec: NamedSerialPort) -> FakeSerial:
        raise PermissionError(13, "Access is denied")

    with pytest.raises(SerialHandoffError) as exc_info:
        verify_all_chamber_ports_released(
            context="before phase sputter",
            ports=(spec,),
            timeout_s=0.0,
            retry_s=0.01,
            serial_factory=factory,
            sleep=lambda _seconds: None,
        )

    message = str(exc_info.value)
    assert "COM9" in message
    assert "next phase was not started" in message
