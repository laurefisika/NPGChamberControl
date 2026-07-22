from npg_chamber.devices.oven_pid import (
    ACK,
    ENQ,
    EOT,
    ETX,
    STX,
    OvenPID,
    format_target_like_current_data,
    parse_frame,
    parse_numeric_ascii,
    xor_bcc,
)


def test_parse_data_frame():
    frame = parse_frame(b"\x02M1000200\x03x")
    assert frame.status == "DATA"
    assert frame.identifier == "M1"
    assert frame.data == "000200"
    assert frame.numeric_value == 200.0


def test_parse_ack():
    frame = parse_frame(ACK)
    assert frame.status == "ACK"


def test_parse_numeric_ascii():
    assert parse_numeric_ascii("000035") == 35.0
    assert parse_numeric_ascii("SV=0020.5C") == 20.5
    assert parse_numeric_ascii("abc") is None


def test_format_target_like_current_data_integer_template():
    assert format_target_like_current_data("000200", 35) == "000035"
    assert format_target_like_current_data("-00020", -5) == "-00005"


def test_format_target_like_current_data_decimal_template():
    assert format_target_like_current_data("0200.0", 35.5) == "0035.5"


def test_build_write_frame():
    pid = OvenPID(serial_instance=object())
    frame = pid.build_write_frame("S1", "000035")
    body = b"S1000035" + ETX
    assert frame == EOT + b"00" + STX + body + xor_bcc(body)


class FakeSerial:
    def __init__(self):
        self.writes = []
        self.responses = [b"\x02S1000200\x03x", ACK, b"\x02S1000201\x03x"]
        self.in_waiting = 64

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def read(self, _n):
        return self.responses.pop(0)

    def close(self):
        pass


def test_set_setpoint_with_fake_serial():
    fake = FakeSerial()
    pid = OvenPID(serial_instance=fake)
    confirmed = pid.set_setpoint_c(201, max_delta_from_current_c=2)
    assert confirmed == 201.0
    assert fake.writes[1] == b"00S1" + ENQ
