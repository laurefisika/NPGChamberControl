from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import serial


PORT = "COM10"

# IMPAC UPP supported baud rates.
BAUD_CANDIDATES = [
    19200,
    9600,
    38400,
    57600,
    115200,
    4800,
    2400,
    1200,
]

# 99 is the UPP global address with response.
# 00 is also tested because it is the usual factory default.
ADDRESSES_TO_TEST = ["99", "00"]

BAUD_CODE_TO_RATE = {
    "0": 1200,
    "1": 2400,
    "2": 4800,
    "3": 9600,
    "4": 19200,
    "5": 38400,
    "6": 57600,
    "8": 115200,
}


@dataclass
class DetectionResult:
    baudrate: int
    address_used: str
    device_name: Optional[str]
    parameter_string: Optional[str]
    temperature_c: Optional[float]


def read_reply(ser: serial.Serial, command: str) -> bytes:
    """Send one read-only UPP query and return the reply including no CR."""
    payload = (command + "\r").encode("ascii")

    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(payload)
    ser.flush()

    # The instrument normally replies very quickly, but leave margin for the
    # USB-RS232 converter and Windows driver.
    raw = ser.read_until(b"\r")
    return raw.rstrip(b"\r\n")


def decode_ascii(raw: bytes) -> Optional[str]:
    if not raw:
        return None
    text = raw.decode("ascii", errors="replace").strip()
    return text or None


def parse_temperature(raw: bytes) -> Optional[float]:
    text = decode_ascii(raw)
    if text is None:
        return None

    if text == "77770":
        raise RuntimeError(
            "Pyrometer returned 77770: internal instrument temperature/error condition."
        )
    if text == "88880":
        raise RuntimeError("Pyrometer returned 88880: measurement overflow.")

    if not text.isdigit():
        raise ValueError(f"Unexpected temperature reply: {raw!r}")

    return int(text) / 10.0


def describe_parameter_string(text: Optional[str]) -> None:
    """
    Decode the 11-digit AApa response when available.

    Digits:
      1-2 emissivity
      3 exposure-time code
      4 maximum-value-storage code
      5 analog-output code
      6-7 internal temperature
      8-9 actual device address
      10 baud-rate code
      11 fixed/reserved
    """
    if text is None:
        print("  Parameters: no reply")
        return

    print(f"  Raw parameter string: {text!r}")

    if len(text) != 11 or not text.isdigit():
        print("  Could not decode the parameter string automatically.")
        return

    emissivity_digits = text[0:2]
    exposure_code = text[2]
    storage_code = text[3]
    analog_code = text[4]
    internal_temp = text[5:7]
    actual_address = text[7:9]
    baud_code = text[9]
    fixed_digit = text[10]

    # In this protocol, "00" in the two-digit emissivity field represents
    # 100%, while 10..99 represent the displayed percentage.
    emissivity_percent = 100 if emissivity_digits == "00" else int(emissivity_digits)

    print(f"  Emissivity: {emissivity_percent}%")
    print(f"  Exposure-time code: {exposure_code}")
    print(f"  Maximum-value-storage code: {storage_code}")
    print(f"  Analog-output code: {analog_code}")
    print(f"  Internal instrument temperature: {int(internal_temp)} deg C")
    print(f"  Actual pyrometer address: {actual_address}")
    print(
        "  Configured baud rate: "
        f"{BAUD_CODE_TO_RATE.get(baud_code, 'unknown')} "
        f"(code {baud_code})"
    )
    print(f"  Final fixed/reserved digit: {fixed_digit}")


def test_connection(baudrate: int, address: str) -> Optional[DetectionResult]:
    with serial.Serial(
        port=PORT,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.50,
        write_timeout=0.50,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    ) as ser:
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass

        print(f"\nTesting baud {baudrate}, address {address}")

        raw_name = read_reply(ser, f"{address}na")
        print(f"  Device query:      sent {address}na, received {raw_name!r}")

        raw_params = read_reply(ser, f"{address}pa")
        print(f"  Parameter query:   sent {address}pa, received {raw_params!r}")

        raw_temp = read_reply(ser, f"{address}ms")
        print(f"  Temperature query: sent {address}ms, received {raw_temp!r}")

        if not any((raw_name, raw_params, raw_temp)):
            return None

        device_name = decode_ascii(raw_name)
        parameter_string = decode_ascii(raw_params)

        temperature_c: Optional[float]
        try:
            temperature_c = parse_temperature(raw_temp)
        except Exception as exc:
            print(f"  Temperature parse warning: {exc}")
            temperature_c = None

        return DetectionResult(
            baudrate=baudrate,
            address_used=address,
            device_name=device_name,
            parameter_string=parameter_string,
            temperature_c=temperature_c,
        )


def detect_pyrometer() -> DetectionResult:
    for baudrate in BAUD_CANDIDATES:
        for address in ADDRESSES_TO_TEST:
            try:
                result = test_connection(baudrate, address)
            except serial.SerialException as exc:
                raise RuntimeError(f"Could not use {PORT}: {exc}") from exc
            except serial.SerialTimeoutException as exc:
                print(f"  Write timeout: {exc}")
                result = None

            if result is not None:
                return result

            time.sleep(0.10)

    raise RuntimeError(
        "COM5 opened, but no UPP reply was received at global address 99 "
        "or address 00 at any supported baud rate. The next checks are the "
        "pyrometer's displayed Adr/Baud settings and the RS232 Tx/Rx/GND cable."
    )


def main() -> None:
    print("IMPAC IPE 140 read-only diagnostic v2")
    print(f"Port: {PORT}")
    print("Serial format: 8 data bits, even parity, 1 stop bit")
    print("Queries used: device name (na), parameters (pa), temperature (ms)")
    print("No pyrometer settings will be changed.")

    result = detect_pyrometer()

    print("\n" + "=" * 58)
    print("COMMUNICATION DETECTED")
    print("=" * 58)
    print(f"Baud rate that replied: {result.baudrate}")
    print(f"Address used for detection: {result.address_used}")
    print(f"Device name: {result.device_name or 'not returned'}")

    describe_parameter_string(result.parameter_string)

    if result.temperature_c is not None:
        print(f"  Raw pyrometer temperature: {result.temperature_c:.1f} deg C")

        estimated_oven_c = (20.259 + result.temperature_c) / 0.5112
        estimated_sample_c = 0.941 * estimated_oven_c - 43.435

        print(f"  Estimated oven temperature: {estimated_oven_c:.1f} deg C")
        print(f"  Estimated sample temperature: {estimated_sample_c:.1f} deg C")
    else:
        print("  Temperature was not decoded, but communication was detected.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nTEST FAILED: {exc}")
    finally:
        input("\nPress Enter to close...")
