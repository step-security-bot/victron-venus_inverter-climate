"""Configuration mistakes must not accidentally enable control."""

from dataclasses import replace

import pytest

from inverter_climate.config import Config, DeviceConfig, Policy


def write_config(tmp_path, contents):
    path = tmp_path / "climate.toml"
    path.write_text(contents)
    return str(path)


def test_config_defaults_to_observation_only(tmp_path):
    config = Config.load(write_config(tmp_path, '[service]\nentity_id = "climate.furnace"\n'))
    assert config.mode == "observe"
    assert config.device.control_enabled is False
    assert config.policy.heating_power_w == 500
    assert config.state_path != config.status_path


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_manual_control_requires_an_explicit_boolean(value):
    with pytest.raises(ValueError, match="control_enabled"):
        DeviceConfig(control_enabled=value)


def test_manual_control_requires_native_device_publishing(tmp_path):
    with pytest.raises(ValueError, match="enabled device publisher"):
        DeviceConfig(enabled=False, control_enabled=True)
    path = write_config(
        tmp_path,
        """
[service]
entity_id = "climate.furnace"
[device]
control_enabled = true
""",
    )
    with pytest.raises(ValueError, match="Venus backend"):
        Config.load(path)


def test_manual_control_does_not_enable_automatic_policy(tmp_path):
    config = Config.load(
        write_config(
            tmp_path,
            """
[service]
entity_id = "climate.furnace"
[energy]
backend = "venus"
solar_paths = ["/Dc/Pv/Power"]
grid_phases = ["L1"]
[device]
control_enabled = true
""",
        )
    )
    assert config.device.control_enabled is True
    assert config.mode == "observe"


def test_explicit_active_mode_and_policy_override(tmp_path):
    config = Config.load(
        write_config(
            tmp_path,
            """
[service]
entity_id = "climate.furnace"
mode = "active"
poll_seconds = 15
[policy]
comfort_min_c = 18
comfort_max_c = 21
boost_delta_c = 1
""",
        )
    )
    assert config.mode == "active"
    assert config.poll_seconds == 15
    assert config.policy.comfort_max_c == 21


@pytest.mark.parametrize(
    "entity",
    ["", "climate.*", "sensor.furnace", "climate.Furnace", "climate.furnace;climate.other", "all"],
)
def test_entity_must_be_one_exact_climate_id(tmp_path, entity):
    with pytest.raises(ValueError):
        Config.load(write_config(tmp_path, f'[service]\nentity_id = "{entity}"\n'))


@pytest.mark.parametrize(
    "settings",
    [
        'mode = "enabled"',
        "poll_seconds = 1",
        "poll_seconds = 61",
        "poll_seconds = true",
        "poll_seconds = nan",
        "actve = true",
        'state_path = "same.json"\nstatus_path = "./same.json"',
        'state_path = "state.json"\nstatus_path = "./state.json.manual"',
        'state_path = "state.json"\nstatus_path = "./state.json.lockfile"',
    ],
)
def test_invalid_service_settings_are_rejected(tmp_path, settings):
    with pytest.raises(ValueError):
        Config.load(write_config(tmp_path, '[service]\nentity_id = "climate.furnace"\n' + settings))


def test_unknown_sections_and_policy_keys_are_not_silently_ignored(tmp_path):
    for extra in ('\n[secret]\ntoken = "example"', "\n[policy]\nstart_sco = 90"):
        with pytest.raises((ValueError, TypeError)):
            Config.load(write_config(tmp_path, '[service]\nentity_id = "climate.furnace"' + extra))


@pytest.mark.parametrize(
    "settings",
    [
        {"comfort_min_c": 4},
        {"comfort_max_c": 31},
        {"comfort_min_c": 19, "comfort_max_c": 19},
        {"boost_delta_c": 0},
        {"boost_delta_c": 2.1},
        {"heating_power_w": 0},
        {"heating_power_w": "500"},
        {"heating_power_w": True},
        {"heating_power_w": float("nan")},
        {"max_import_w": -1},
        {"start_soc": 101},
        {"stop_soc": 90, "start_soc": 90},
        {"minimum_boost_seconds": 299},
        {"maximum_boost_seconds": 7201},
        {"minimum_boost_seconds": 1801, "maximum_boost_seconds": 1800},
        {"command_interval_seconds": 299},
        {"manual_hold_seconds": 299},
        {"surplus_hold_seconds": 29},
        {"confirmation_seconds": 29},
        {"confirmation_seconds": 301},
        {"max_energy_age_seconds": 4},
        {"max_energy_age_seconds": 301},
    ],
)
def test_policy_rejects_unsafe_or_mistyped_bounds(settings):
    with pytest.raises(ValueError):
        replace(Policy(), **settings)
