"""Small manual hardware check using the common device modules.

Run from the project root after installing editable mode:

    python diagnostic_tools/check_all_common_devices.py

The script only reads values by default. It does not change PID setpoints or
Keysight output state.
"""

from __future__ import annotations

from npg_chamber.config.ports import DEFAULT_PORTS
from npg_chamber.devices import (
    ArduinoTemperatureReader,
    KeysightE3632A,
    OvenPID,
    QMBController,
    XGS600Gauge,
)


def try_read(label: str, fn):
    try:
        print(f"{label}: {fn()}")
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"{label}: ERROR: {exc}")


def read_oven_pid_pair():
    with OvenPID.from_port_config() as pid:
        return pid.read_process_value_c(), pid.read_setpoint_c()


def main() -> None:
    print("Common device read-only check")
    print("================================")

    try_read("Oven PID PV/SV", read_oven_pid_pair)
    try_read("XGS600 pressure mbar", lambda: XGS600Gauge.from_port_config().read_pressure_mbar())
    try_read("Keysight V/I", lambda: KeysightE3632A.from_port_config().read_voltage_current())
    try_read(
        "CK-1 Arduino temp C",
        lambda: ArduinoTemperatureReader.from_port_config().read_temperature_c(),
    )
    try_read(
        "CK-1 QMB thickness/rate",
        lambda: QMBController.from_port_config(DEFAULT_PORTS.ck1_qmb).read_both(),
    )
    try_read(
        "Sample QMB thickness/rate",
        lambda: QMBController.from_port_config(DEFAULT_PORTS.sample_qmb).read_both(),
    )


if __name__ == "__main__":
    main()
