"""Boundary tests for the observations that authorize thermostat writes."""

from copy import deepcopy

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
def test_missing_device_precision_uses_conservative_native_step(unit, step):
    data = climate_payload()
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
