#!/usr/bin/env python3
"""Safe diagnostic tool for the oven PID module.

Default behaviour is read-only: it reads M1 and S1 and prints them. Writing S1 is
possible only when both --setpoint and --confirm-heater-power-off are supplied.
"""

from __future__ import annotations

import argparse
import sys
import time

from npg_chamber.devices.oven_pid import OvenPID, OvenPIDSafetyError, OvenPIDProtocolError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check communication with the oven PID.")
    parser.add_argument("--port", default="COM9", help="PID serial port. Default: COM9")
    parser.add_argument("--baudrate", type=int, default=9600, help="PID baudrate. Default: 9600")
    parser.add_argument("--address", default="00", help="PID address. Default: 00")
    parser.add_argument("--setpoint", type=float, help="Optional new S1 setpoint in ºC.")
    parser.add_argument(
        "--confirm-heater-power-off",
        action="store_true",
        help="Required before writing S1. Confirms heater power is disabled.",
    )
    parser.add_argument(
        "--max-delta",
        type=float,
        default=1.0,
        help="Maximum allowed S1 change for this diagnostic write. Default: 1 ºC.",
    )
    parser.add_argument(
        "--monitor-seconds",
        type=float,
        default=5.0,
        help="Seconds to monitor PV/SV after the check. Default: 5 s.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.setpoint is not None and not args.confirm_heater_power_off:
        print("Refusing to write S1: add --confirm-heater-power-off after disabling heater power.")
        return 2

    try:
        with OvenPID(port=args.port, baudrate=args.baudrate, address=args.address) as pid:
            pv, sv = pid.read_pv_sv()
            print(f"Initial M1/PV: {pv}")
            print(f"Initial S1/SV: {sv}")

            if args.setpoint is not None:
                confirmed = pid.set_setpoint_c(
                    args.setpoint,
                    verify=True,
                    max_delta_from_current_c=args.max_delta,
                )
                print(f"Confirmed S1/SV: {confirmed}")

            deadline = time.time() + max(0.0, args.monitor_seconds)
            while time.time() < deadline:
                pv, sv = pid.read_pv_sv()
                print(f"M1/PV: {pv} | S1/SV: {sv}")
                time.sleep(1.0)

    except (OvenPIDSafetyError, OvenPIDProtocolError, OSError) as exc:
        print(f"PID diagnostic failed: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
