from __future__ import annotations

import csv
import math
import time
from datetime import datetime
from pathlib import Path

import serial


# =============================================================================
# FIXED COMMUNICATION SETTINGS CONFIRMED BY THE DIAGNOSTIC
# =============================================================================

PORT = "COM10"
BAUDRATE = 38400
ADDRESS = "00"

# Test duration and sampling rate.
DURATION_MINUTES = 5.0
SAMPLE_INTERVAL_S = 1.0

# Calibration equations supplied for the chamber.
# T_oven = (20.259 + T_pyrometer) / 0.5112
# T_sample = 0.941 * T_oven - 43.435
def estimate_oven_temperature(pyrometer_c: float) -> float:
    return (20.259 + pyrometer_c) / 0.5112


def estimate_sample_temperature(oven_c: float) -> float:
    return 0.941 * oven_c - 43.435


def read_reply(ser: serial.Serial, command: str) -> bytes:
    """Send one read-only IMPAC UPP query and return the reply without CR/LF."""
    payload = (command + "\r").encode("ascii")

    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(payload)
    ser.flush()

    raw = ser.read_until(b"\r")
    return raw.rstrip(b"\r\n")


def parse_temperature(raw: bytes) -> float:
    """Decode the five-digit IMPAC temperature reply in tenths of a degree."""
    if not raw:
        raise TimeoutError("No reply")

    text = raw.decode("ascii", errors="replace").strip()

    if text == "77770":
        raise RuntimeError("Instrument returned 77770")
    if text == "88880":
        raise RuntimeError("Measurement overflow (88880)")
    if not text.isdigit():
        raise ValueError(f"Unexpected reply {raw!r}")

    return int(text) / 10.0


def main() -> None:
    output_dir = Path(__file__).resolve().parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"pyrometer_COM10_monitor_{stamp}.csv"

    duration_s = max(1.0, DURATION_MINUTES * 60.0)
    start_monotonic = time.monotonic()
    next_sample_at = start_monotonic
    sample_number = 0
    successful_reads = 0
    failed_reads = 0
    temperatures: list[float] = []

    print("=" * 72)
    print("IMPAC IPE 140 CONTINUOUS READ-ONLY MONITOR")
    print("=" * 72)
    print(f"Port:              {PORT}")
    print(f"Baud rate:         {BAUDRATE}")
    print("Serial format:      8 data bits, even parity, 1 stop bit")
    print(f"Pyrometer address: {ADDRESS}")
    print(f"Duration:           {DURATION_MINUTES:.1f} minutes")
    print(f"Sampling interval:  {SAMPLE_INTERVAL_S:.1f} second")
    print(f"CSV output:         {csv_path}")
    print()
    print("Only the read-only temperature command '00ms' will be sent.")
    print("Press Ctrl+C to finish early. COM10 will still be closed cleanly.")
    print()
    print(
        f"{'Time':19s}  {'Raw pyro':>9s}  {'Est. oven':>10s}  "
        f"{'Est. sample':>12s}  Status"
    )
    print("-" * 72)

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    "timestamp",
                    "elapsed_s",
                    "sample_number",
                    "raw_pyrometer_c",
                    "estimated_oven_c",
                    "estimated_sample_c",
                    "status",
                    "raw_reply",
                ]
            )

            with serial.Serial(
                port=PORT,
                baudrate=BAUDRATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.60,
                write_timeout=0.60,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            ) as ser:
                try:
                    ser.dtr = False
                    ser.rts = False
                except Exception:
                    pass

                while True:
                    now = time.monotonic()
                    if now - start_monotonic >= duration_s:
                        break

                    if now < next_sample_at:
                        time.sleep(min(0.05, next_sample_at - now))
                        continue

                    sample_number += 1
                    timestamp = datetime.now()
                    elapsed_s = now - start_monotonic
                    raw_reply = b""

                    try:
                        raw_reply = read_reply(ser, f"{ADDRESS}ms")
                        pyro_c = parse_temperature(raw_reply)
                        oven_c = estimate_oven_temperature(pyro_c)
                        sample_c = estimate_sample_temperature(oven_c)

                        successful_reads += 1
                        temperatures.append(pyro_c)
                        status = "OK"

                        print(
                            f"{timestamp:%Y-%m-%d %H:%M:%S}  "
                            f"{pyro_c:9.1f}  {oven_c:10.1f}  {sample_c:12.1f}  OK"
                        )

                        writer.writerow(
                            [
                                timestamp.isoformat(timespec="milliseconds"),
                                f"{elapsed_s:.3f}",
                                sample_number,
                                f"{pyro_c:.3f}",
                                f"{oven_c:.3f}",
                                f"{sample_c:.3f}",
                                status,
                                raw_reply.decode("ascii", errors="replace"),
                            ]
                        )

                    except Exception as exc:
                        failed_reads += 1
                        status = f"ERROR: {exc}"

                        print(
                            f"{timestamp:%Y-%m-%d %H:%M:%S}  "
                            f"{'nan':>9s}  {'nan':>10s}  {'nan':>12s}  {status}"
                        )

                        writer.writerow(
                            [
                                timestamp.isoformat(timespec="milliseconds"),
                                f"{elapsed_s:.3f}",
                                sample_number,
                                "nan",
                                "nan",
                                "nan",
                                status,
                                raw_reply.decode("ascii", errors="replace"),
                            ]
                        )

                    csv_file.flush()
                    next_sample_at += SAMPLE_INTERVAL_S

                    # Prevent a backlog if Windows pauses the program briefly.
                    if next_sample_at < time.monotonic() - SAMPLE_INTERVAL_S:
                        next_sample_at = time.monotonic() + SAMPLE_INTERVAL_S

    except KeyboardInterrupt:
        print("\nMonitoring stopped by the user.")
    except serial.SerialException as exc:
        print(f"\nSERIAL ERROR: {exc}")
        print("Make sure no other program is using COM10.")
    finally:
        # The serial context manager closes COM10 before this summary is printed.
        print("\n" + "=" * 72)
        print("MONITOR SUMMARY")
        print("=" * 72)
        print(f"Successful readings: {successful_reads}")
        print(f"Failed readings:     {failed_reads}")

        total = successful_reads + failed_reads
        if total:
            print(f"Success rate:        {100.0 * successful_reads / total:.1f}%")

        if temperatures:
            print(f"Minimum raw temp:    {min(temperatures):.1f} deg C")
            print(f"Maximum raw temp:    {max(temperatures):.1f} deg C")
            print(f"Average raw temp:    {sum(temperatures) / len(temperatures):.1f} deg C")

        print(f"CSV saved at:        {csv_path}")
        print("COM10 is now closed.")
        input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
