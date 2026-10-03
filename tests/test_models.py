"""Boundary tests for the observations that authorize thermostat writes."""

from copy import deepcopy
from dataclasses import replace

import pytest

from inverter_climate.models import (
    Climate,
    Energy,
    InvalidObservation,
    celsius,
    native_temperature,
    number,
)


def climate_payload():
    return {
        "entity_id": "climate.furnace",
        "state": "heat",
        "attributes": {
            "hvac_action": "heating",
            "current_temperature": 65,
            "temperature": 66,
            "min_temp": 50,
            "max_temp": 90,
            "target_temp_step": 1,
            "supported_features": 1,
            "hvac_modes": ["heat", "off"],
        },
    }


def energy_payload():
    return {
        "schema_version": 1,
        "mqtt_connected": True,
        "generated_at": 990,
        "metrics": {
            name: {
                "status": "fresh",
                "value": value,
                "age_seconds": 10,
                "unit": unit,
                "sources": ["N/test/system/0/Ac/Grid/L1/Power"],
            }
            for name, value, unit in (
                ("battery_soc", 92, "%"),
                ("solar_power", 1500, "W"),
                ("grid_power", -650, "W"),
                ("battery_power", 200, "W"),
            )
        },
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        "500",
        float("nan"),
        float("inf"),
        [],
        pytest.param(10**400, id="integer_overflow"),
    ],
)
def test_measurements_must_be_actual_finite_numbers(value):
    with pytest.raises(InvalidObservation):
        number(value)


def test_fahrenheit_observation_and_native_command_round_trip():
    climate = Climate.parse(climate_payload(), "climate.furnace", "°F")
    assert climate.current_c == pytest.approx(18.333333)
    assert climate.target_c == pytest.approx(18.888889)
    assert climate.step_c == pytest.approx(5 / 9)
    assert climate.min_c == 10
    assert climate.supports_target
    assert climate.preset == "none"
    assert native_temperature(climate.target_c + climate.step_c, climate.unit) == 67


@pytest.mark.parametrize("unit", ["F", "C", "K", "", None])
def test_unknown_units_never_silently_become_celsius(unit):
    with pytest.raises(InvalidObservation):
        celsius(20, unit)
    with pytest.raises(InvalidObservation):
        native_temperature(20, unit)


@pytest.mark.parametrize("unit, step", [("°F", 5 / 9), ("°C", 0.5)])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_missing_device_precision_uses_conservative_native_step(unit, step, explicit_null):
    data = climate_payload()
    if explicit_null:
        data["attributes"]["target_temp_step"] = None
    else:
        del data["attributes"]["target_temp_step"]
    climate = Climate.parse(data, "climate.furnace", unit)
    assert climate.step_c == pytest.approx(step)


@pytest.mark.parametrize("state", [None, "unknown", "unavailable"])
def test_unavailable_thermostat_is_rejected(state):
    data = climate_payload()
    data["state"] = state
    with pytest.raises(InvalidObservation):
        Climate.parse(data, "climate.furnace", "°F")


def test_wrong_thermostat_cannot_authorize_control():
    with pytest.raises(InvalidObservation, match="identity"):
        Climate.parse(climate_payload(), "climate.other", "°F")


@pytest.mark.parametrize(
    "key, value",
    [
        ("temperature", None),
        ("temperature", 100),
        ("current_temperature", "unknown"),
        ("current_temperature", -1000),
        ("min_temp", 90),
        ("max_temp", 45),
        ("target_temp_step", 0),
        ("target_temp_step", -1),
        ("target_temp_step", float("nan")),
        ("supported_features", True),
        ("supported_features", -1),
        ("supported_features", "1"),
    ],
)
def test_malformed_device_attributes_are_rejected(key, value):
    data = climate_payload()
    data["attributes"][key] = value
    with pytest.raises(InvalidObservation):
        Climate.parse(data, "climate.furnace", "°F")


def test_target_capability_is_not_inferred_from_other_feature_bits():
    data = climate_payload()
    data["attributes"]["supported_features"] = 2 | 16 | 128
    assert not Climate.parse(data, "climate.furnace", "°F").supports_target


def test_capabilities_use_advertised_modes_and_support_bit_independently():
    data = climate_payload()
    climate = Climate.parse(data, "climate.furnace", "°F")
    assert climate.hvac_modes == ("heat", "off")
    assert climate.supports_heat_off
    assert climate.can_set_temperature
    assert climate.hvac_mode_command("off") == "off"
    data["attributes"]["supported_features"] = 0
    without_target = Climate.parse(data, "climate.furnace", "°F")
    assert without_target.supports_heat_off
    assert not without_target.can_set_temperature
    assert without_target.hvac_mode_command("heat") == "heat"


@pytest.mark.parametrize("modes", [None, []])
def test_missing_advertised_modes_disable_manual_control_without_losing_observation(modes):
    data = climate_payload()
    data["attributes"]["hvac_modes"] = modes
    climate = Climate.parse(data, "climate.furnace", "°F")
    assert climate.current_c == pytest.approx(18.333333)
    assert climate.hvac_modes == ()
    assert not climate.supports_heat_off
    assert not climate.can_set_temperature
    with pytest.raises(InvalidObservation):
        climate.hvac_mode_command("heat")
    with pytest.raises(InvalidObservation):
        climate.temperature_command(20)


@pytest.mark.parametrize("modes", ["heat", ["heat", "heat"], [None], [True], ["Heat"], ["unknown"]])
def test_invalid_mode_advertisements_cannot_authorize_manual_commands(modes):
    data = climate_payload()
    data["attributes"]["hvac_modes"] = modes
    with pytest.raises(InvalidObservation, match="HVAC modes"):
        Climate.parse(data, "climate.furnace", "°F")


@pytest.mark.parametrize("mode", ["off", "auto", "heat_cool", "fan_only", "dry"])
def test_missing_single_target_preserves_room_observation_and_explicit_heat_off(mode):
    data = climate_payload()
    data["state"] = mode
    data["attributes"]["temperature"] = None
    climate = Climate.parse(data, "climate.furnace", "°F")
    assert climate.target_c is None
    assert climate.current_c == pytest.approx(18.333333)
    assert climate.supports_heat_off
    assert climate.hvac_mode_command("heat") == "heat"
    assert climate.hvac_mode_command("off") == "off"
    assert not climate.can_set_temperature
    with pytest.raises(InvalidObservation):
        climate.temperature_command(20)


@pytest.mark.parametrize("mode", ["cool", "auto", "heat_cool", "fan_only", "dry"])
def test_other_valid_hvac_modes_remain_observable_but_reject_single_heating_target(mode):
    data = climate_payload()
    data["state"] = mode
    climate = Climate.parse(data, "climate.furnace", "°F")
    assert climate.mode == mode
    assert climate.current_c == pytest.approx(18.333333)
    with pytest.raises(InvalidObservation):
        climate.temperature_command(20)


@pytest.mark.parametrize("mode", [None, True, "Heat", "unknown", "arbitrary_mode"])
def test_invalid_current_hvac_mode_is_not_coerced_into_an_observation(mode):
    data = climate_payload()
    data["state"] = mode
    with pytest.raises(InvalidObservation):
        Climate.parse(data, "climate.furnace", "°F")


@pytest.mark.parametrize("value_c, native", [(10, 50), (20, 68), (30, 86), (19.4444, 67)])
def test_fahrenheit_manual_command_uses_device_step_with_celsius_roundoff(value_c, native):
    climate = Climate.parse(climate_payload(), "climate.furnace", "°F")
    assert climate.temperature_command(value_c) == native


def test_celsius_manual_target_uses_real_device_range_and_minimum_anchored_step():
    data = climate_payload()
    data["attributes"].update(
        {
            "current_temperature": 20,
            "temperature": 20.25,
            "min_temp": 10.25,
            "max_temp": 30.25,
            "target_temp_step": 0.5,
        }
    )
    climate = Climate.parse(data, "climate.furnace", "°C")
    for target in (10.25, 19.75, 30.25):
        assert climate.temperature_command(target) == target
    for target in (10, 20, 30.5):
        with pytest.raises(InvalidObservation):
            climate.temperature_command(target)


@pytest.mark.parametrize("target", [9, 33, 19.5, 20.001, True, None, "20", float("nan"), 10**400])
def test_out_of_range_off_step_and_malformed_manual_targets_are_rejected(target):
    climate = Climate.parse(climate_payload(), "climate.furnace", "°F")
    with pytest.raises(InvalidObservation):
        climate.temperature_command(target)


def test_dbus_boolean_cannot_become_a_numeric_manual_request():
    dbus_boolean = type("Boolean", (int,), {"__module__": "dbus"})
    value = dbus_boolean(1)
    with pytest.raises(InvalidObservation):
        number(value)


def test_preset_and_incomplete_capability_block_manual_target_without_changing_mode():
    climate = Climate.parse(climate_payload(), "climate.furnace", "°F")
    for changed in (
        replace(climate, preset="eco"),
        replace(climate, target_c=None),
        replace(climate, supports_target=False),
        replace(climate, hvac_modes=("off",)),
    ):
        assert not changed.can_set_temperature
        with pytest.raises(InvalidObservation):
            changed.temperature_command(20)


@pytest.mark.parametrize("mode", ["cool", "auto", "heat_cool", "", "Heat", True, None])
def test_manual_mode_commands_are_explicit_heat_off_only(mode):
    climate = Climate.parse(climate_payload(), "climate.furnace", "°F")
    with pytest.raises(InvalidObservation):
        climate.hvac_mode_command(mode)


def test_mode_command_requires_requested_advertised_capability_not_current_state():
    climate = Climate.parse(climate_payload(), "climate.furnace", "°F")
    off_only = replace(climate, mode="heat", hvac_modes=("off",))
    assert off_only.hvac_mode_command("off") == "off"
    assert not off_only.supports_heat_off
    with pytest.raises(InvalidObservation):
        off_only.hvac_mode_command("heat")


def test_energy_preserves_grid_export_and_battery_charge_signs():
    energy = Energy.parse(energy_payload(), now=1000, max_age=120)
    assert energy == Energy(soc=92, solar_w=1500, grid_w=-650, battery_w=200)


@pytest.mark.parametrize("generated", [879, 1006, "990", None, float("nan")])
def test_response_timestamp_is_required_and_bounded(generated):
    data = energy_payload()
    data["generated_at"] = generated
    with pytest.raises(InvalidObservation):
        Energy.parse(data, now=1000, max_age=120)


@pytest.mark.parametrize("version", [True, 1.0, 2, "1", None])
def test_energy_schema_version_is_exact(version):
    data = energy_payload()
    data["schema_version"] = version
    with pytest.raises(InvalidObservation):
        Energy.parse(data, now=1000, max_age=120)


@pytest.mark.parametrize("connected", [False, "true", 1, None])
def test_disconnected_or_ambiguous_mqtt_is_rejected(connected):
    data = energy_payload()
    data["mqtt_connected"] = connected
    with pytest.raises(InvalidObservation):
        Energy.parse(data, now=1000, max_age=120)


@pytest.mark.parametrize("name", ["battery_soc", "solar_power", "grid_power", "battery_power"])
@pytest.mark.parametrize(
    "key, value",
    [
        ("status", "stale"),
        ("status", "missing"),
        ("value", None),
        ("value", True),
        ("value", "500"),
        ("value", float("inf")),
        pytest.param("value", 10**400, id="integer_overflow"),
        ("age_seconds", -1),
        ("age_seconds", 111),
        ("unit", "kW"),
        ("sources", []),
        ("sources", "N/test/system/0"),
    ],
)
def test_each_required_metric_must_be_fresh_typed_and_attributable(name, key, value):
    data = deepcopy(energy_payload())
    data["metrics"][name][key] = value
    with pytest.raises(InvalidObservation):
        Energy.parse(data, now=1000, max_age=120)


def test_response_age_and_metric_age_are_added():
    data = energy_payload()
    data["generated_at"] = 920
    data["metrics"]["grid_power"]["age_seconds"] = 41
    with pytest.raises(InvalidObservation, match="grid_power expired"):
        Energy.parse(data, now=1000, max_age=120)


@pytest.mark.parametrize(
    "name, value", [("battery_soc", -1), ("battery_soc", 101), ("solar_power", -1)]
)
def test_nonphysical_soc_and_solar_are_rejected(name, value):
    data = energy_payload()
    data["metrics"][name]["value"] = value
    with pytest.raises(InvalidObservation):
        Energy.parse(data, now=1000, max_age=120)


def test_zero_is_a_valid_measurement_but_missing_is_not():
    data = energy_payload()
    data["metrics"]["grid_power"]["value"] = 0
    assert Energy.parse(data, now=1000, max_age=120).grid_w == 0
    del data["metrics"]["grid_power"]
    with pytest.raises(InvalidObservation):
        Energy.parse(data, now=1000, max_age=120)
