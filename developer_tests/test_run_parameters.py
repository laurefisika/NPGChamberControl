from __future__ import annotations


import pytest

from npg_chamber.config.run_parameters import (
    AUTOMATION_PARAMETERS_ENV,
    all_default_values,
    encode_overrides,
    load_phase_overrides,
    non_default_overrides,
    specs_for_phase,
    validate_phase_values,
    PYROMETER_PARAMETERS_ENV,
    encode_pyrometer_settings,
    load_pyrometer_settings,
    pyrometer_default_values,
    pyrometer_specs,
    validate_pyrometer_values,
)


def test_all_packaged_parameter_defaults_validate() -> None:
    for phase, values in all_default_values().items():
        assert validate_phase_values(phase, values) == values
        assert non_default_overrides(phase, values) == {}


def test_run_only_override_round_trip() -> None:
    values = all_default_values()["heat"]
    values["HEATING_TRIGGER_TEMP_C"] = 250.0
    values["PID_KP_A_PER_C"] = 0.0025
    raw = encode_overrides("heat", values)
    loaded = load_phase_overrides("heat", {AUTOMATION_PARAMETERS_ENV: raw})
    assert loaded == {
        "HEATING_TRIGGER_TEMP_C": 250.0,
        "PID_KP_A_PER_C": 0.0025,
    }


def test_payload_is_phase_specific() -> None:
    raw = encode_overrides("heat", all_default_values()["heat"])
    with pytest.raises(RuntimeError, match="prepared for phase"):
        load_phase_overrides("dpdbba", {AUTOMATION_PARAMETERS_ENV: raw})


def test_hard_safety_limits_and_ports_are_not_editable() -> None:
    editable_keys = {
        spec.key
        for phase in all_default_values()
        for spec in specs_for_phase(phase)
    }
    assert "KEYSIGHT_HARD_STOP_A" not in editable_keys
    assert "KEYSIGHT_HARD_STOP_V" not in editable_keys
    assert "KEYSIGHT_OCP_A" not in editable_keys
    assert "PID_PORT" not in editable_keys
    assert "xgs600_port" not in editable_keys



def test_phase13_automatic_current_limits_keep_clearance_below_fixed_hard_stop() -> None:
    for phase in ("heat", "dpdbba"):
        specs = {spec.key: spec for spec in specs_for_phase(phase)}
        defaults = all_default_values()[phase]
        assert defaults["KEYSIGHT_SOFT_WARNING_A"] == pytest.approx(0.660)
        assert specs["KEYSIGHT_SOFT_WARNING_A"].maximum == pytest.approx(0.675)
        assert specs["KEYSIGHT_BASE_WORK_CURRENT_A"].maximum == pytest.approx(0.675)
        assert specs["KEYSIGHT_START_CURRENT_A"].maximum == pytest.approx(0.675)

def test_phase_relationship_validation() -> None:
    values = all_default_values()["sputter"]
    values["target_ar_pressure_mbar"] = 1e-4
    values["pressure_warning_mbar"] = 5e-5
    with pytest.raises(ValueError, match="target pressure"):
        validate_phase_values("sputter", values)


def test_phase4_minutes_are_converted_to_seconds_for_child_script() -> None:
    first_wait = next(spec for spec in specs_for_phase("anneal") if spec.key == "INITIAL_WAIT_S")
    assert first_wait.internal_value("7.5") == 450.0
    assert first_wait.format_display(450.0) == "7.5"

def test_phase2_coscon_targets_are_editable_with_operator_units() -> None:
    specs = {spec.key: spec for spec in specs_for_phase("sputter")}
    energy = specs["coscon_energy_v"]
    emission = specs["coscon_emission_a"]

    assert energy.format_display(2250.0) == "2250"
    assert energy.internal_value("2000") == 2000.0

    assert emission.format_display(0.010) == "10"
    assert emission.internal_value("8.5") == pytest.approx(0.0085)

    values = all_default_values()["sputter"]
    values["coscon_energy_v"] = 2000.0
    values["coscon_emission_a"] = 0.0085
    raw = encode_overrides("sputter", values)
    loaded = load_phase_overrides("sputter", {AUTOMATION_PARAMETERS_ENV: raw})
    assert loaded == {
        "coscon_energy_v": 2000.0,
        "coscon_emission_a": pytest.approx(0.0085),
    }


def test_phase2_can_start_without_initial_degas_as_a_run_only_setting() -> None:
    specs = {spec.key: spec for spec in specs_for_phase("sputter")}
    skip_degas = specs["start_without_degassing"]

    assert skip_degas.kind == "bool"
    assert skip_degas.default is False

    values = all_default_values()["sputter"]
    values["start_without_degassing"] = True
    raw = encode_overrides("sputter", values)
    loaded = load_phase_overrides("sputter", {AUTOMATION_PARAMETERS_ENV: raw})

    assert loaded == {"start_without_degassing": True}


def test_phase2_coscon_target_editor_limits() -> None:
    values = all_default_values()["sputter"]
    values["coscon_energy_v"] = 3100.0
    with pytest.raises(ValueError, match="COSCON energy target"):
        validate_phase_values("sputter", values)

    values = all_default_values()["sputter"]
    values["coscon_emission_a"] = 0.001
    with pytest.raises(ValueError, match="COSCON emission target"):
        validate_phase_values("sputter", values)



def test_pyrometer_profile_round_trip_and_validated_defaults() -> None:
    defaults = pyrometer_default_values()
    assert validate_pyrometer_values(defaults) == defaults
    raw = encode_pyrometer_settings(defaults)
    assert load_pyrometer_settings({PYROMETER_PARAMETERS_ENV: raw}) == defaults


def test_pyrometer_custom_profile_allows_material_specific_calibration() -> None:
    values = pyrometer_default_values()
    values.update({
        "profile_name": "Custom material",
        "emissivity_percent": 35.0,
        "sample_slope": 1.25,
        "sample_intercept_c": 12.0,
        "minimum_valid_pyrometer_c": 110.0,
        "default_view": "sample",
    })
    assert validate_pyrometer_values(values) == values


def test_validated_au_profile_cannot_be_silently_modified() -> None:
    values = pyrometer_default_values()
    values["emissivity_percent"] = 35.0
    with pytest.raises(ValueError, match="Custom material"):
        validate_pyrometer_values(values)


def test_pyrometer_communication_settings_are_not_operator_editable() -> None:
    editable = {spec.key for spec in pyrometer_specs()}
    assert "port" not in editable
    assert "baudrate" not in editable
    assert "address" not in editable


def test_pyrometer_custom_calibration_rejects_nonphysical_slope_and_cutoff() -> None:
    values = pyrometer_default_values()
    values["profile_name"] = "Custom material"
    values["sample_slope"] = 0.0
    with pytest.raises(ValueError, match="Sample calibration slope"):
        validate_pyrometer_values(values)

    values = pyrometer_default_values()
    values["profile_name"] = "Custom material"
    values["minimum_valid_pyrometer_c"] = 40.0
    with pytest.raises(ValueError, match="Minimum calibrated pyrometer temperature"):
        validate_pyrometer_values(values)


def test_rate_feedback_relationships_are_validated() -> None:
    values = all_default_values()["heat"]
    values["EVAPORATION_CONTROL_MODE"] = "compound"
    assert validate_phase_values("heat", values)["EVAPORATION_CONTROL_MODE"] == "compound"

    values = all_default_values()["heat"]
    values["RATE_CONTROL_MAX_TEMP_C"] = values["HEATING_TRIGGER_TEMP_C"] - 1.0
    with pytest.raises(ValueError, match="temperature ceiling"):
        validate_phase_values("heat", values)

    values = all_default_values()["dpdbba"]
    values["RATE_PID_ACTIVATION_A_PER_S"] = values["CK1_RATE_TARGET_A_PER_S"]
    with pytest.raises(ValueError, match="activation threshold"):
        validate_phase_values("dpdbba", values)


def test_rate_control_safety_parameters_are_available_in_both_phases() -> None:
    expected = {
        "EVAPORATION_CONTROL_MODE",
        "RATE_CONTROL_MAX_TEMP_C",
        "TEMP_WATCHDOG_MAX_TEMP_C",
        "KEYSIGHT_SOFT_WARNING_A",
        "RATE_PID_SIGNAL_TIMEOUT_S",
        "RATE_PID_MAX_UP_STEP_A",
        "RATE_PID_MAX_DOWN_STEP_A",
        "COMPOUND_TEMP_GUARD_BAND_C",
    }
    for phase in ("heat", "dpdbba"):
        assert expected <= {spec.key for spec in specs_for_phase(phase)}


def test_phase_01_and_03_new_safety_defaults_and_relationships() -> None:
    for phase in ("heat", "dpdbba"):
        defaults = all_default_values()[phase]
        assert defaults["RATE_CONTROL_MAX_TEMP_C"] == 250.0
        assert defaults["TEMP_WATCHDOG_MAX_TEMP_C"] == 255.0
        assert defaults["KEYSIGHT_SOFT_WARNING_A"] == 0.660
        assert defaults["PID_TEMP_BAND_C"] == 0.7
        if phase == "heat":
            assert defaults["CALIBRATION_TARGET_SAMPLE_A"] == 2.0

        invalid = dict(defaults)
        invalid["TEMP_WATCHDOG_MAX_TEMP_C"] = invalid["RATE_CONTROL_MAX_TEMP_C"]
        with pytest.raises(ValueError, match="watchdog maximum temperature"):
            validate_phase_values(phase, invalid)

        invalid = dict(defaults)
        invalid["KEYSIGHT_BASE_WORK_CURRENT_A"] = invalid["KEYSIGHT_SOFT_WARNING_A"] + 0.001
        with pytest.raises(ValueError, match="maximum automatic current cap"):
            validate_phase_values(phase, invalid)
