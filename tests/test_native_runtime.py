"""Native backend selection and reduced flash writes retain control guarantees."""

from dataclasses import replace

import pytest

from inverter_climate.config import Config, EnergyConfig, Policy
from inverter_climate.controller import State
from inverter_climate.models import Energy, InvalidObservation
from inverter_climate.service import Service, make_energy_client
from inverter_climate.storage import load_state, save_state

SOLAR = ("Dc/Pv/Power", "Ac/PvOnGrid/L1/Power")
GRID = ("L1", "L2")
BINDING = "example-binding"


class Clock:
    now = 10000.0

    def __call__(self):
        return self.now


class FakeHA:
    target = 18

    def __init__(self):
        self.writes = []

    def get_config(self):
        return {"unit_system": {"temperature": "°C"}}

    def get_climate(self, entity_id):
        return {
            "entity_id": entity_id,
            "state": "heat",
            "attributes": {
                "temperature": self.target,
                "current_temperature": 17,
                "min_temp": 10,
                "max_temp": 30,
                "supported_features": 1,
                "target_temp_step": 0.5,
                "hvac_action": "heating",
            },
        }

    def set_temperature(self, entity_id, temperature):
        self.writes.append((entity_id, temperature))


class NativeSource:
    def __init__(self, clock):
        self.clock = clock

    def get_energy(self):
        return {
            "schema_version": 1,
            "source_type": "venus_dbus",
            "source_connected": True,
            "provenance": "local_service_read",
            "generated_at": self.clock(),
            "metrics": {
                name: {
                    "value": value,
                    "unit": unit,
                    "status": "fresh",
                    "age_seconds": 0,
                    "sources": [f"system/0/{path}"],
                }
                for name, value, unit, path in (
                    ("battery_soc", 95, "%", "Dc/Battery/Soc"),
                    ("battery_power", 100, "W", "Dc/Battery/Power"),
                    ("grid_power", -700, "W", "Ac/Grid/L1/Power"),
                    ("solar_power", 1500, "W", "Dc/Pv/Power"),
                )
            },
        }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    clock, ha = Clock(), FakeHA()
    source = NativeSource(clock)
    config = Config(
        "climate.example",
        "observe",
        30,
        tmp_path / "state.json",
        tmp_path / "status.json",
        Policy(),
        EnergyConfig("venus", SOLAR, GRID),
    )
    saves = []

    def recorded_save(path, binding, state):
        saves.append(clock())
        save_state(path, binding, state)

    monkeypatch.setattr("inverter_climate.service.save_state", recorded_save)
    return config, clock, ha, source, saves


def test_stable_observation_does_not_rewrite_flash_on_every_poll(runtime):
    config, clock, ha, source, saves = runtime
    service = Service(config, ha, source, BINDING, clock=clock)
    for offset in range(0, 301, 30):
        clock.now = 10000 + offset
        result = service.tick()
    assert saves == [10000]
    assert result["decision"]["action"] == "would_boost"
    assert result["energy_backend"] == "venus"
    assert ha.writes == []


def test_restart_requalifies_surplus_even_when_journal_contains_a_qualified_window(runtime):
    config, clock, ha, source, saves = runtime
    config = replace(config, mode="active")
    save_state(
        config.state_path, BINDING, State(surplus_since=clock() - 500, last_tick=clock() - 30)
    )
    service = Service(config, ha, source, BINDING, clock=clock)
    assert service.state.surplus_since is None
    assert service.tick()["decision"]["reason"] == "stabilizing_surplus"
    assert ha.writes == []


def test_manual_hold_is_persisted_immediately_and_survives_restart(runtime):
    config, clock, ha, source, saves = runtime
    service = Service(config, ha, source, BINDING, clock=clock)
    service.tick()
    clock.now += 15
    ha.target = 18.5
    assert service.tick()["decision"]["reason"] == "external_change_respected"
    assert saves == [10000, 10015]
    saved = load_state(config.state_path, BINDING)
    assert saved.hold_until == clock() + config.policy.manual_hold_seconds
    restarted = Service(config, ha, source, BINDING, clock=clock)
    assert restarted.tick()["decision"]["reason"] == "manual_or_cooldown_hold"
    assert saves == [10000, 10015]
    assert ha.writes == []


def test_owned_state_checkpoints_once_a_minute_despite_stable_observations(runtime):
    config, clock, ha, source, saves = runtime
    config = replace(config, mode="active")
    ha.target = 18.5
    save_state(
        config.state_path,
        BINDING,
        State(
            phase="boosted",
            baseline_c=18,
            boosted_c=18.5,
            since=clock() - 300,
            last_command=clock() - 300,
            last_tick=clock(),
            observed_target_c=18.5,
            observed_mode="heat",
            observed_preset="none",
        ),
    )
    service = Service(config, ha, source, BINDING, clock=clock)
    for offset in (0, 30, 60, 90, 120):
        clock.now = 10000 + offset
        assert service.tick()["decision"]["reason"] == "boost_running"
    assert saves == [10060, 10120]
    assert load_state(config.state_path, BINDING).last_tick == 10120
    assert ha.writes == []


def test_ownership_confirmation_is_immediate_even_between_periodic_checkpoints(runtime):
    config, clock, ha, source, saves = runtime
    config = replace(config, mode="active")
    save_state(
        config.state_path,
        BINDING,
        State(
            phase="pending_boost",
            baseline_c=18,
            boosted_c=18.5,
            since=clock() - 30,
            last_command=clock() - 30,
            last_tick=clock() - 30,
            observed_target_c=18,
            observed_mode="heat",
            observed_preset="none",
        ),
    )
    ha.target = 18.5
    service = Service(config, ha, source, BINDING, clock=clock)
    assert service.tick()["phase"] == "boosted"
    assert saves == [10000]
    assert load_state(config.state_path, BINDING).phase == "boosted"
    assert ha.writes == []


def test_final_ha_read_cannot_authorize_boost_with_energy_that_expired_during_preflight(
    runtime,
    monkeypatch,
):
    config, clock, ha, source, _ = runtime
    config = replace(config, mode="active")
    service = Service(config, ha, source, BINDING, clock=clock)
    service.state = State(surplus_since=clock() - 200, last_tick=clock() - 30)
    original_energy = source.get_energy
    original_climate = ha.get_climate
    reads = 0

    def almost_expired_energy():
        raw = original_energy()
        raw["generated_at"] -= config.policy.max_energy_age_seconds - 5
        return raw

    def delayed_final_climate(entity_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            clock.now += 10
        return original_climate(entity_id)

    monkeypatch.setattr(source, "get_energy", almost_expired_energy)
    monkeypatch.setattr(ha, "get_climate", delayed_final_climate)
    result = service.tick()
    assert reads == 2
    assert result["decision"]["reason"] == "command_preflight_failed"
    assert service.state.phase == "idle"
    assert ha.writes == []
    assert load_state(config.state_path, BINDING).phase == "idle"


def test_venus_backend_does_not_request_gateway_credentials(runtime, monkeypatch):
    config, _, _, _, _ = runtime
    captured = {}
    expected = object()

    def construct(**kwargs):
        captured.update(kwargs)
        return expected

    def unexpected_credential(_):
        pytest.fail("native energy must not request gateway credentials")

    monkeypatch.setattr("inverter_climate.venus.VenusEnergyClient", construct)
    monkeypatch.setattr("inverter_climate.service.required_env", unexpected_credential)
    assert make_energy_client(config) is expected
    assert captured == {"solar_paths": SOLAR, "grid_phases": GRID, "timeout_seconds": 5}


def test_gateway_backend_honors_configured_energy_timeout(runtime, monkeypatch):
    config, _, _, _, _ = runtime
    config = replace(config, energy=EnergyConfig(timeout_seconds=2))
    captured = {}

    def construct(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("inverter_climate.service.GatewayClient", construct)
    monkeypatch.setattr("inverter_climate.service.required_env", lambda _: "example")
    make_energy_client(config)
    assert captured["timeout_seconds"] == 2


def test_native_connection_branch_does_not_require_mqtt(runtime):
    _, clock, _, source, _ = runtime
    assert Energy.parse(source.get_energy(), clock(), 120).solar_w == 1500


@pytest.mark.parametrize("connected", [False, None, 1, "true"])
def test_native_connection_state_is_exact_and_cannot_fall_back_to_mqtt(runtime, connected):
    _, clock, _, source, _ = runtime
    raw = source.get_energy()
    raw["source_connected"] = connected
    raw["mqtt_connected"] = True
    with pytest.raises(InvalidObservation, match="D-Bus source disconnected"):
        Energy.parse(raw, clock(), 120)


@pytest.mark.parametrize("source_type", ["venus", "dbus", "gateway", "", True, 1])
def test_unknown_source_type_cannot_authorize_energy(runtime, source_type):
    _, clock, _, source, _ = runtime
    raw = source.get_energy()
    raw["source_type"] = source_type
    raw["mqtt_connected"] = True
    with pytest.raises(InvalidObservation, match="source type"):
        Energy.parse(raw, clock(), 120)


def test_gateway_without_source_type_still_requires_mqtt_even_with_native_flag(runtime):
    _, clock, _, source, _ = runtime
    raw = source.get_energy()
    del raw["source_type"]
    with pytest.raises(InvalidObservation, match="MQTT disconnected"):
        Energy.parse(raw, clock(), 120)
    raw["mqtt_connected"] = True
    assert Energy.parse(raw, clock(), 120).grid_w == -700


def test_native_config_parses_explicit_sources_to_immutable_tuples(tmp_path):
    path = tmp_path / "climate.toml"
    path.write_text("""
[service]
entity_id = "climate.example"
[energy]
backend = "venus"
solar_paths = ["Dc/Pv/Power", "Ac/PvOnGrid/L1/Power"]
grid_phases = ["L1", "L2"]
timeout_seconds = 2.5
""")
    config = Config.load(str(path))
    assert config.energy == EnergyConfig("venus", SOLAR, GRID, 2.5)
    assert config.mode == "observe"


@pytest.mark.parametrize(
    "settings",
    [
        'backend = "venus"',
        'backend = "venus"\nsolar_paths = "Dc/Pv/Power"\ngrid_phases = ["L1"]',
        'backend = "venus"\nsolar_paths = ["Dc/Pv/Power"]\ngrid_phases = [1]',
        'backend = "gateway"\nsolar_paths = ["Dc/Pv/Power"]',
        'backend = "automatic"',
        "timeout_seconds = 0",
        "timeout_seconds = 11",
        "timeout_seconds = nan",
        "timeout_seconds = true",
        "timeuot_seconds = 5",
    ],
)
def test_invalid_energy_config_fails_closed(tmp_path, settings):
    path = tmp_path / "climate.toml"
    path.write_text('[service]\nentity_id = "climate.example"\n[energy]\n' + settings)
    with pytest.raises(ValueError):
        Config.load(str(path))
