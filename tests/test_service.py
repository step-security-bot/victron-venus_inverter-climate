import copy
import json
from dataclasses import replace

import pytest

from inverter_climate.clients import IntegrationError
from inverter_climate.config import Config, Policy
from inverter_climate.controller import State
from inverter_climate.service import Service
from inverter_climate.storage import load_state, save_state

NOW = 1_800_000_000.0
ENTITY = "climate.example"


class FakeHA:
    def __init__(self):
        self.target = 17.0
        self.mode = "heat"
        self.calls = []
        self.fail = False
        self.fail_write = False
        self.before_read = None
        self.before_write = None

    def get_config(self):
        if self.fail:
            raise IntegrationError("HA unavailable")
        return {"unit_system": {"temperature": "°C"}}

    def get_climate(self, entity):
        if self.before_read:
            self.before_read()
        return {
            "entity_id": entity,
            "state": self.mode,
            "attributes": {
                "current_temperature": 16,
                "temperature": self.target,
                "min_temp": 10,
                "max_temp": 32,
                "supported_features": 401,
                "hvac_action": "idle",
                "preset_mode": "none",
            },
        }

    def set_temperature(self, entity, temperature):
        if self.before_write:
            self.before_write()
        self.calls.append((entity, temperature))
        if self.fail_write:
            raise IntegrationError("uncertain write")
        self.target = temperature


class FakeGateway:
    def __init__(self):
        self.fail = False
        self.payload = {
            "schema_version": 1,
            "generated_at": NOW,
            "mqtt_connected": True,
            "metrics": {},
        }
        for key, value, unit in (
            ("battery_soc", 95, "%"),
            ("solar_power", 2000, "W"),
            ("grid_power", -700, "W"),
            ("battery_power", 0, "W"),
        ):
            self.payload["metrics"][key] = {
                "value": value,
                "unit": unit,
                "status": "fresh",
                "age_seconds": 0,
                "sources": ["system/0/example"],
            }

    def get_energy(self):
        if self.fail:
            raise IntegrationError("gateway unavailable")
        return copy.deepcopy(self.payload)


@pytest.fixture
def setup_service(tmp_path):
    config = Config(
        ENTITY, "active", 30, tmp_path / "state.json", tmp_path / "status.json", Policy()
    )
    ha, gateway = FakeHA(), FakeGateway()
    service = Service(config, ha, gateway, "example-binding", clock=lambda: NOW)
    service.state = State(surplus_since=NOW - 200, last_tick=NOW - 30)
    return service, ha, gateway


def own_boost(service, ha):
    ha.target = 17.5
    service.state = State(
        phase="boosted",
        baseline_c=17,
        boosted_c=17.5,
        since=NOW - 600,
        last_command=NOW - 600,
        observed_target_c=17.5,
        observed_mode="heat",
        observed_preset="none",
    )


def test_observe_never_posts(setup_service):
    service, ha, _ = setup_service
    service.config = replace(service.config, mode="observe")
    result = service.tick()
    assert result["decision"]["action"] == "would_boost"
    assert ha.calls == []
    assert service.state.phase == "idle"


def test_intent_durable_before_post_and_confirmed_on_next_tick(setup_service):
    service, ha, _ = setup_service

    def check_journal():
        saved = load_state(service.config.state_path, "example-binding")
        assert saved.phase == "pending_boost"
        assert saved.baseline_c == 17
        assert saved.boosted_c == 17.5

    ha.before_write = check_journal
    assert service.tick()["phase"] == "pending_boost"
    assert ha.calls == [(ENTITY, 17.5)]
    assert service.tick()["phase"] == "boosted"
    assert len(ha.calls) == 1


def test_journal_failure_sends_no_command(setup_service, monkeypatch):
    service, ha, _ = setup_service

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("inverter_climate.service.save_state", fail)
    with pytest.raises(OSError):
        service.tick()
    assert not ha.calls


def test_manual_change_during_preflight_cancels_boost(setup_service):
    service, ha, _ = setup_service
    count = 0

    def changed():
        nonlocal count
        count += 1
        if count == 2:
            ha.target = 18

    # A prior observation lets the fresh read detect the manual change.
    service.state.observed_target_c = 17
    service.state.observed_mode = "heat"
    service.state.observed_preset = "none"
    ha.before_read = changed
    result = service.tick()
    assert result["decision"]["reason"] == "external_change_respected"
    assert not ha.calls


def test_restore_works_when_gateway_is_down(setup_service):
    service, ha, gateway = setup_service
    own_boost(service, ha)
    gateway.fail = True
    result = service.tick()
    assert result["decision"]["action"] == "restore"
    assert ha.calls == [(ENTITY, 17)]
    assert service.tick()["decision"]["reason"] == "baseline_restored"


def test_failed_post_survives_restart_without_retry(setup_service):
    service, ha, gateway = setup_service
    ha.fail_write = True
    result = service.tick()
    assert "command_outcome_unconfirmed_no_retry" in result["errors"]
    restarted = Service(service.config, ha, gateway, "example-binding", clock=lambda: NOW + 150)
    restarted.tick()
    assert len(ha.calls) == 1
    assert restarted.state.phase == "pending_boost"


def test_late_boost_after_timeout_is_released_if_energy_lost(setup_service):
    service, ha, gateway = setup_service
    ha.fail_write = True
    service.tick()
    ha.target = 17.5
    ha.fail_write = False
    gateway.fail = True
    service.clock = lambda: NOW + 300
    result = service.tick()
    assert result["decision"]["action"] == "restore"
    assert ha.calls == [(ENTITY, 17.5), (ENTITY, 17)]


def test_preflight_ha_failure_sends_no_write_or_pending_intent(setup_service):
    service, ha, _ = setup_service
    original = ha.get_config
    count = 0

    def second_read_fails():
        nonlocal count
        count += 1
        if count > 1:
            raise IntegrationError("HA unavailable")
        return original()

    ha.get_config = second_read_fails
    result = service.tick()
    assert result["decision"]["reason"] == "command_preflight_failed"
    assert service.state.phase == "idle"
    assert not ha.calls


def test_release_does_not_start_new_boost(setup_service):
    service, ha, _ = setup_service
    assert service.tick(release=True)["decision"]["action"] == "wait"
    assert not ha.calls


def test_release_only_restores_owned_target(setup_service):
    service, ha, _ = setup_service
    own_boost(service, ha)
    ha.target = 18
    result = service.tick(release=True)
    assert result["decision"]["reason"] == "external_change_respected"
    assert not ha.calls


def test_observe_does_not_restore_owned_active_state(setup_service):
    service, ha, gateway = setup_service
    own_boost(service, ha)
    service.config = replace(service.config, mode="observe")
    gateway.fail = True
    assert service.tick()["decision"]["action"] == "would_restore"
    assert not ha.calls


def test_status_is_atomic_private_and_has_no_connection_credentials(setup_service):
    service, _, _ = setup_service
    service.tick()
    data = json.loads(service.config.status_path.read_text())
    assert data["mode"] == "active"
    assert "entity_id" not in data
    assert service.config.status_path.stat().st_mode & 0o777 == 0o600


def test_existing_journal_identity_cannot_control_another_thermostat(setup_service):
    service, ha, gateway = setup_service
    save_state(service.config.state_path, "some-other-identity", State())
    with pytest.raises(ValueError):
        Service(service.config, ha, gateway, "example-binding")
    assert not ha.calls
