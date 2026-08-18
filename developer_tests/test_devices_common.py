from npg_chamber.devices.arduino import parse_temperature_c
from npg_chamber.devices.keysight import KeysightE3632A, parse_optional_float
from npg_chamber.devices.qmb import COMMANDS, build_command, calculate_checksum, parse_numeric_response
from npg_chamber.devices.xgs600 import safe_float_from_text


class FakeSerial:
    def __init__(self, read_chunks=None, readline_chunks=None):
        self.writes = []
        self.read_chunks = list(read_chunks or [])
        self.readline_chunks = list(readline_chunks or [])
        self.in_waiting = 64
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def read(self, _n=1):
        if self.read_chunks:
            return self.read_chunks.pop(0)
        return b""

    def readline(self):
        if self.readline_chunks:
            return self.readline_chunks.pop(0)
        return b""

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True


def test_qmb_checksum_matches_legacy_formula():
    payload = b"\x10\x80S"
    assert calculate_checksum(payload) == b">3"
    assert build_command("thickness") == COMMANDS["thickness"]
    assert COMMANDS["thickness"] == b"\x02\x10\x80S>3\r"
    assert COMMANDS["rate"] == b"\x02\x10\x80T>4\r"
    assert COMMANDS["zero"] == b"\x02\x10\x80B=2\r"


def test_qmb_parse_numeric_response_uses_legacy_crop():
    assert parse_numeric_response(b"abc12.34xyz") == 12.34
    assert parse_numeric_response(b"") is None
    assert parse_numeric_response(b"abcNOxyz") is None


def test_keysight_write_query_and_readback_sequence():
    fake = FakeSerial(readline_chunks=[b"2.300\n", b"0.640\n"])
    psu = KeysightE3632A("COM17", serial_instance=fake)

    readback = psu.read_voltage_current()

    assert readback.voltage_v == 2.3
    assert readback.current_a == 0.64
    assert fake.writes == [
        b"system:remote\n",
        b"measure:voltage?\n",
        b"measure:current?\n",
        b"system:local\n",
    ]


def test_keysight_clamped_current_is_explicit():
    fake = FakeSerial()
    psu = KeysightE3632A("COM17", serial_instance=fake)

    commanded = psu.set_current(0.8, max_current_a=0.66)

    assert commanded == 0.66
    assert fake.writes[-1] == b"CURR 0.660\n"


def test_parse_helpers():
    assert parse_optional_float(" 1.23 ") == 1.23
    assert parse_optional_float("bad") is None
    assert safe_float_from_text("> +1.20E-05 mbar") == 1.20e-5
    assert parse_temperature_c("CK1 temp = 242.5 C") == 242.5
    assert parse_temperature_c("no value") is None
