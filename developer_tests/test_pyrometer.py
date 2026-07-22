from __future__ import annotations

from dataclasses import replace

import pytest

from npg_chamber.devices.pyrometer import (
    ImpacIPE140,
    PyrometerProfile,
    PyrometerSerialConfig,
)


class FakeSerial:
    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.is_open = True
        self.writes: list[bytes] = []
        self.input_resets = 0
        self.output_resets = 0
        self.closed = False

    def reset_input_buffer(self) -> None:
        self.input_resets += 1

    def reset_output_buffer(self) -> None:
        self.output_resets += 1

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        return None

    def read_until(self, _terminator: bytes) -> bytes:
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def client_with_fake(replies: list[bytes]) -> tuple[ImpacIPE140, FakeSerial]:
    client = ImpacIPE140(PyrometerSerialConfig())
    fake = FakeSerial(replies)
    client.ser = fake  # type: ignore[assignment]
    return client, fake


def test_confirmed_serial_defaults_match_the_hardware_test() -> None:
    config = PyrometerSerialConfig()
    assert config.port == "COM10"
    assert config.baudrate == 38400
    assert config.address == "00"


def test_temperature_query_and_tenths_of_degree_parsing(monkeypatch) -> None:
    monkeypatch.setattr("npg_chamber.devices.pyrometer.time.sleep", lambda _seconds: None)
    client, fake = client_with_fake([b"00558\r"])
    assert client.read_temperature_c() == pytest.approx(55.8)
    assert fake.writes == [b"00ms\r"]


def test_parameter_reply_is_used_for_emissivity_readback(monkeypatch) -> None:
    monkeypatch.setattr("npg_chamber.devices.pyrometer.time.sleep", lambda _seconds: None)
    client, fake = client_with_fake([b"10300280050\r"])
    assert client.read_emissivity_percent() == pytest.approx(10.0)
    assert fake.writes == [b"00pa\r"]


def test_emissivity_write_is_verified_by_parameter_readback(monkeypatch) -> None:
    monkeypatch.setattr("npg_chamber.devices.pyrometer.time.sleep", lambda _seconds: None)
    client, fake = client_with_fake([b"ok\r", b"35300280050\r"])
    assert client.set_emissivity_percent(35.0, verify=True) == pytest.approx(35.0)
    assert fake.writes == [b"00em0350\r", b"00pa\r"]


def test_emissivity_encoding_limits_and_whole_percent_requirement() -> None:
    assert ImpacIPE140._format_emissivity(10.0) == "0100"
    assert ImpacIPE140._format_emissivity(11.0) == "0110"
    assert ImpacIPE140._format_emissivity(100.0) == "1000"
    with pytest.raises(ValueError):
        ImpacIPE140._format_emissivity(10.5)
    with pytest.raises(ValueError):
        ImpacIPE140._format_emissivity(9.0)
    with pytest.raises(ValueError):
        ImpacIPE140._format_emissivity(101.0)


def test_profile_keeps_below_cutoff_estimate_but_marks_warning() -> None:
    profile = PyrometerProfile()
    estimated = profile.estimated_sample_c(89.9)
    assert estimated == pytest.approx(1.69959 * 89.9 + 28.20193)
    assert profile.is_within_calibrated_range(89.9) is False
    assert "WARNING" in profile.calibration_status(89.9)
    assert profile.calibration_status(90.0) == "OK"


def test_custom_profile_changes_material_calibration_without_control_side_effects() -> None:
    profile = replace(
        PyrometerProfile(),
        profile_name="Graphite holder",
        emissivity_percent=35.0,
        sample_slope=1.25,
        sample_intercept_c=12.0,
        minimum_valid_pyrometer_c=100.0,
    )
    assert profile.estimated_sample_c(120.0) == pytest.approx(162.0)


def test_close_resets_buffers_and_releases_serial_port() -> None:
    client, fake = client_with_fake([])
    client.close()
    assert fake.input_resets == 1
    assert fake.output_resets == 1
    assert fake.closed is True
    assert client.ser is None


def test_ensure_emissivity_avoids_unnecessary_write(monkeypatch) -> None:
    monkeypatch.setattr("npg_chamber.devices.pyrometer.time.sleep", lambda _seconds: None)
    client, fake = client_with_fake([b"10300280050\r"])
    confirmed, changed = client.ensure_emissivity_percent(10.0)
    assert confirmed == pytest.approx(10.0)
    assert changed is False
    assert fake.writes == [b"00pa\r"]


def test_ensure_emissivity_updates_only_when_needed(monkeypatch) -> None:
    monkeypatch.setattr("npg_chamber.devices.pyrometer.time.sleep", lambda _seconds: None)
    client, fake = client_with_fake([b"10300280050\r", b"ok\r", b"35300280050\r"])
    confirmed, changed = client.ensure_emissivity_percent(35.0)
    assert confirmed == pytest.approx(35.0)
    assert changed is True
    assert fake.writes == [b"00pa\r", b"00em0350\r", b"00pa\r"]
