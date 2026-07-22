#!/usr/bin/env python3
"""Controlled S1 setpoint write test for an RKC-type PID controller.

IMPORTANT
---------
This script is intended ONLY to verify whether the controller accepts an S1
write followed by an immediate readback. It includes safeguards against an
imprudent test:

1) It requires explicit confirmation that heater power is disabled.
2) By default, it permits only a small +/- 1.0 change from the current SV.
3) It reads M1 and S1 before and after the write.
4) It immediately reads S1 back to verify that the change was accepted.
5) It monitors PV and SV for several seconds after the test.

The frame follows the RKC protocol's "Selecting" procedure:
EOT + address + STX + identifier + data + ETX + BCC.
The BCC is the XOR of every byte from IDENTIFIER through ETX, inclusive.
"""

import argparse
import math
import sys
import time
from datetime import datetime

import serial

EOT = b"\x04"
ENQ = b"\x05"
STX = b"\x02"
ETX = b"\x03"
ACK = b"\x06"
NAK = b"\x15"


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def xor_bcc(identifier_plus_data_plus_etx: bytes) -> bytes:
    x = 0
    for b in identifier_plus_data_plus_etx:
        x ^= b
    return bytes([x])


def open_serial(port: str, baud: int, timeout: float) -> serial.Serial:
    return serial.Serial(
        port=port,
        baudrate=baud,
        timeout=timeout,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
    )


def read_identifier_raw(ser: serial.Serial, address: str, identifier: str, wait_s: float = 0.15) -> bytes:
    ser.reset_input_buffer()
    ser.write(EOT)
    time.sleep(0.05)
    ser.write(address.encode("ascii") + identifier.encode("ascii") + ENQ)
    time.sleep(wait_s)
    return ser.read(ser.in_waiting or 64)


def parse_rkc_data_frame(raw: bytes):
    """Parse a response such as:

      STX + IDENT(2) + DATA + ETX + BCC

    Observed example:
      b'\\x02M1000057\\x03}'
      ident='M1', data='000057'
    """
    if raw == b"":
        return {"status": "NO_RESPONSE", "raw": raw}

    if raw == NAK:
        return {"status": "NAK", "raw": raw}

    if raw == ACK:
        return {"status": "ACK", "raw": raw}

    if raw == EOT:
        return {"status": "EOT", "raw": raw}

    if len(raw) >= 5 and raw[0:1] == STX:
        try:
            etx_index = raw.index(ETX)
        except ValueError:
            return {"status": "UNKNOWN_FRAME", "raw": raw, "decoded": raw.decode(errors="ignore")}

        core = raw[1:etx_index]  # IDENT + DATA
        if len(core) < 2:
            return {"status": "SHORT_FRAME", "raw": raw, "decoded": raw.decode(errors="ignore")}

        ident = core[:2].decode(errors="ignore")
        data = core[2:].decode(errors="ignore")
        return {
            "status": "DATA",
            "raw": raw,
            "decoded": raw.decode(errors="ignore"),
            "ident": ident,
            "data": data,
        }

    return {"status": "UNKNOWN", "raw": raw, "decoded": raw.decode(errors="ignore")}


def parse_numeric_ascii(data: str):
    s = data.strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        pass

    # Last attempt: keep only digits, a sign, and a decimal point.
    allowed = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    if allowed in ("", "-", ".", "-."):
        return None
    try:
        return float(allowed)
    except ValueError:
        return None


def format_target_like_current_data(current_data: str, target_value: float) -> str:
    """Format the new S1 value to match the currently returned data.

    If the read SV is ``000035``, return ``000036`` for a target of 36.
    If it is ``0012.5``, preserve the decimal places and field width.
    """
    template = current_data.strip()
    if not template:
        raise ValueError("No current S1 data are available to infer the format.")

    negative = template.startswith("-")
    body = template[1:] if negative else template

    if "." in body:
        left, right = body.split(".", 1)
        decimals = len(right)
        width_left = len(left)
        scaled = round(target_value, decimals)
        fmt = f"{{:0{width_left}.{decimals}f}}"
        text = fmt.format(scaled)
        if negative and target_value < 0 and not text.startswith("-"):
            text = "-" + text
        return text

    # Without a decimal point, use an integer with the same width.
    width = len(body)
    if not float(target_value).is_integer():
        raise ValueError(
            f"The controller returned S1 without a decimal point ('{template}'). "
            f"Use an integer target for this test."
        )
    ivalue = int(round(target_value))
    sign = "-" if ivalue < 0 else ""
    digits = str(abs(ivalue)).zfill(width)
    return sign + digits


def write_s1_frame(ser: serial.Serial, address: str, data_text: str) -> bytes:
    body = b"S1" + data_text.encode("ascii") + ETX
    bcc = xor_bcc(body)
    frame = EOT + address.encode("ascii") + STX + body + bcc

    ser.reset_input_buffer()
    ser.write(frame)
    time.sleep(0.20)
    return ser.read(1)


def ack_name(raw: bytes) -> str:
    if raw == ACK:
        return "ACK"
    if raw == NAK:
        return "NAK"
    if raw == EOT:
        return "EOT"
    if raw == b"":
        return "NO_RESPONSE"
    return repr(raw)


def read_value(ser: serial.Serial, address: str, identifier: str):
    parsed = parse_rkc_data_frame(read_identifier_raw(ser, address, identifier))
    value = None
    if parsed.get("status") == "DATA":
        value = parse_numeric_ascii(parsed.get("data", ""))
    return parsed, value


def main():
    parser = argparse.ArgumentParser(
        description="Controlled S1 setpoint write test with immediate verification."
    )
    parser.add_argument("--port", default="COM9", help="Serial port. Default: COM9.")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate. Default: 9600.")
    parser.add_argument("--address", default="00", help="Two-digit RKC address. Default: 00.")
    parser.add_argument("--target", type=float, required=True, help="New S1 target setpoint.")
    parser.add_argument("--timeout", type=float, default=0.5, help="Serial timeout in seconds.")
    parser.add_argument(
        "--max-step",
        type=float,
        default=1.0,
        help="Maximum permitted change from the current SV. Default: 1.0.",
    )
    parser.add_argument(
        "--allow-large-step",
        action="store_true",
        help="Permit a change greater than --max-step.",
    )
    parser.add_argument(
        "--monitor-seconds",
        type=int,
        default=10,
        help="Seconds to monitor PV and SV after the write.",
    )
    parser.add_argument("--i-confirm-heater-power-is-disabled", action="store_true",
                        help="Confirm that heater power is physically disabled.")
    args = parser.parse_args()

    print("=" * 72)
    print("CONTROLLED S1 SETPOINT WRITE TEST")
    print("DO NOT run unless heater power is physically disabled.")
    print("=" * 72)
    print(f"Port     : {args.port}")
    print(f"Baud rate: {args.baud}")
    print(f"Address  : {args.address}")
    print(f"Target S1: {args.target}")
    print()

    if not args.i_confirm_heater_power_is_disabled:
        print("ABORTED: the required confirmation flag is missing:")
        print("  --i-confirm-heater-power-is-disabled")
        sys.exit(2)

    typed = input("Type exactly YES, HEATER DISABLED to continue: ").strip()
    if typed != "YES, HEATER DISABLED":
        print("ABORTED: invalid safety confirmation.")
        sys.exit(2)

    try:
        ser = open_serial(args.port, args.baud, args.timeout)
    except Exception as e:
        print(f"ERROR opening the port: {e}")
        sys.exit(1)

    print("\nPort opened successfully.\n")

    try:
        m1_before, pv_before = read_value(ser, args.address, "M1")
        s1_before, sv_before = read_value(ser, args.address, "S1")

        print("=== Initial readback ===")
        print(f"M1 | status={m1_before.get('status'):>11} | raw={m1_before.get('raw')} | data={m1_before.get('data')} | PV={pv_before}")
        print(f"S1 | status={s1_before.get('status'):>11} | raw={s1_before.get('raw')} | data={s1_before.get('data')} | SV={sv_before}")

        if s1_before.get("status") != "DATA" or sv_before is None:
            print("\nABORTED: S1 could not be read before the write.")
            sys.exit(1)

        delta = abs(args.target - sv_before)
        if delta > args.max_step and not args.allow_large_step:
            print(f"\nABORTED: the requested change is too large ({sv_before} -> {args.target}, Δ={delta}).")
            print(f"By default, only changes <= {args.max_step} are permitted.")
            print("For an independently reviewed safe test, add --allow-large-step.")
            sys.exit(2)

        data_text = format_target_like_current_data(s1_before["data"], args.target)
        body = b"S1" + data_text.encode("ascii") + ETX
        bcc = xor_bcc(body)
        frame = EOT + args.address.encode("ascii") + STX + body + bcc

        print("\n=== Frame to be sent ===")
        print(f"data_text : {data_text!r}")
        print(f"frame repr: {frame!r}")
        print(f"frame hex : {frame.hex(' ')}")
        print()

        go = input("Send the S1 frame? Type SEND to continue: ").strip()
        if go != "SEND":
            print("Cancelled by the user.")
            sys.exit(0)

        reply = write_s1_frame(ser, args.address, data_text)
        print("\n=== Immediate write response ===")
        print(f"raw={reply!r} | status={ack_name(reply)}")

        time.sleep(0.20)
        s1_after, sv_after = read_value(ser, args.address, "S1")
        m1_after, pv_after = read_value(ser, args.address, "M1")

        print("\n=== Immediate readback ===")
        print(f"S1 | status={s1_after.get('status'):>11} | raw={s1_after.get('raw')} | data={s1_after.get('data')} | SV={sv_after}")
        print(f"M1 | status={m1_after.get('status'):>11} | raw={m1_after.get('raw')} | data={m1_after.get('data')} | PV={pv_after}")

        if reply == ACK and sv_after is not None and math.isclose(sv_after, args.target, abs_tol=0.001):
            print("\nRESULT: ACK received and the S1 readback matches the target.")
            print("This experimentally confirms that the PID accepts this S1 write frame.")
        elif reply == ACK and sv_after is not None:
            print("\nRESULT: ACK received, but the S1 readback does NOT exactly match the target.")
            print("The controller may have rounded, limited, locked, or reformatted the value.")
        elif reply == NAK:
            print("\nRESULT: NAK. The controller rejected the write.")
        else:
            print("\nRESULT: no clear confirmation yet. Inspect ACK/NAK and the S1 readback.")

        if args.monitor_seconds > 0:
            print("\n=== PV/SV monitoring after the test ===")
            end_time = time.time() + args.monitor_seconds
            while time.time() < end_time:
                m1_now, pv_now = read_value(ser, args.address, "M1")
                s1_now, sv_now = read_value(ser, args.address, "S1")
                print(f"{ts()} | PV={pv_now} | SV={sv_now} | raw_M1={m1_now.get('raw')} | raw_S1={s1_now.get('raw')}")
                time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nInterrupted by the user.")
    finally:
        try:
            ser.close()
            print("Port closed.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
