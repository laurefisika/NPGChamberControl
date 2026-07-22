from __future__ import annotations

import time
from typing import Optional

import serial


PORT = "COM5"
ADDRESS = "00"

# Read-only baud-rate candidates.
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


def parse_temperature_response(raw: bytes) -> Optional[float]:
    """Parse a numeric IMPAC temperature response in tenths of a degree."""
    text = raw.decode("ascii", errors="ignore").strip()

    if not text:
        return None

    if text == "77770":
        raise RuntimeError(
            "Pyrometer returned 77770: internal temperature or measuring error."
        )

    if text == "88880":
        raise RuntimeError("Pyrometer returned 88880: temperature overflow.")

    if not text.isdigit():
        raise ValueError(f"Unexpected pyrometer response: {raw!r}")

    return int(text) / 10.0


def query_temperature(
    port: str,
    baudrate: int,
    address: str = "00",
) -> Optional[float]:
    command = f"{address}ms\r".encode("ascii")

    with serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.7,
        write_timeout=0.7,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    ) as ser:
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass

        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(command)
        ser.flush()

        raw = ser.read_until(b"\r")
        print(
            f"Baud {baudrate:>6}: "
            f"sent {command!r}, received {raw!r}"
        )

        if not raw:
            return None

        return parse_temperature_response(raw)


def detect_baudrate() -> tuple[int, float]:
    for baudrate in BAUD_CANDIDATES:
        try:
            temperature_c = query_temperature(
                port=PORT,
                baudrate=baudrate,
                address=ADDRESS,
            )
        except (
            serial.SerialException,
            serial.SerialTimeoutException,
            ValueError,
            RuntimeError,
        ) as exc:
            print(f"  Error at {baudrate}: {exc}")
            time.sleep(0.2)
            continue

        if temperature_c is not None:
            return baudrate, temperature_c

        time.sleep(0.2)

    raise RuntimeError(
        "No valid reply was received. Check that COM5 is free, "
        "the device address is 00, RS-232 is selected, and the "
        "USB-serial cable is correctly connected."
    )


def main() -> None:
    print("IMPAC IPE 140 read-only communication test")
    print(f"Port: {PORT}")
    print(f"Address tried: {ADDRESS}")
    print("Serial format: 8 data bits, even parity, 1 stop bit\n")

    baudrate, temperature_c = detect_baudrate()

    print("\nCommunication successful")
    print(f"Detected baud rate: {baudrate}")
    print(f"Pyrometer temperature: {temperature_c:.1f} deg C")

    estimated_oven_c = (20.259 + temperature_c) / 0.5112
    estimated_sample_c = 0.941 * estimated_oven_c - 43.435

    print(f"Intermediate calibrated oven value: {estimated_oven_c:.1f} deg C")
    print(f"Estimated sample temperature: {estimated_sample_c:.1f} deg C")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nTEST FAILED: {exc}")
    finally:
        input("\nPress Enter to close...")
