from npg_chamber.config.ports import DEFAULT_PORTS, as_legacy_device_info


def test_ports_shape():
    info = as_legacy_device_info(DEFAULT_PORTS)
    assert info["Oven PID temperature"]["port"] == "COM9"
    assert info["Keysight power supply"]["baud_rate"] == 9600
