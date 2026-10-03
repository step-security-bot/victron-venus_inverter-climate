"""Explicit manual commands preserve durable ownership and never retry uncertainty."""

from dataclasses import replace

import pytest

from inverter_climate.clients import IntegrationError
from inverter_climate.config import Config, DeviceConfig, EnergyConfig, Policy
from inverter_climate.control import ControlBroker, load_intent, manual_path, save_intent
from inverter_climate.controller import State
from inverter_climate.service import Service
from inverter_climate.storage import identity, load_state, save_state


class Clock:
    now = 10000.0

    def __call__(self):
        return self.now


class HA:
    def __init__(self):
        self.mode = "heat"
        self.target = 18.0
        self.preset = "none"
        self.modes = ["heat", "off"]
        self.unit = "°C"
        self.calls = []
        self.fail = False
        self.fail_write = False
        self.apply = True
        self.before_post = None

    def get_config(self):
        if self.fail:
            raise IntegrationError("offline")
        return {"unit_system": {"temperature": self.unit}}

    def get_climate(self, entity):
        return {
            "entity_id": entity,
            "state": self.mode,
            "attributes": {
                "current_temperature": 17 if self.unit == "°C" else 62,
                "temperature": self.target,
                "min_temp": 10 if self.unit == "°C" else 50,
                "max_temp": 30 if self.unit == "°C" else 86,
                "target_temp_step": 0.5 if self.unit == "°C" else 1,
                "supported_features": 401,
                "preset_mode": self.preset,
                "hvac_modes": self.modes,
                "hvac_action": "idle",
            },
        }

    def _post(self, kind, value):
        if self.before_post:
            self.before_post()
        self.calls.append((kind, value))
        if self.fail_write:
            raise IntegrationError("uncertain")
        if self.apply:
            if kind == "temperature":
                self.target = value
            else:
                self.mode = value
                self.target = None if value == "off" else 18

    def set_temperature(self, _entity, value):
        self._post("temperature", value)

    def set_hvac_mode(self, _entity, value):
        self._post("mode", value)


class Energy:
    def get_energy(self):
        raise IntegrationError("energy unavailable")


@pytest.fixture
def runtime(tmp_path):
    clock, ha = Clock(), HA()
    config = Config(
        "climate.example",
        "observe",
        30,
        tmp_path / "state.json",
        tmp_path / "status.json",
        Policy(),
        EnergyConfig("venus", ("Dc/Pv/Power",), ("L1",)),
        DeviceConfig(control_enabled=True),
    )
    broker = ControlBroker(True, clock=clock)
    service = Service(config, ha, Energy(), "binding", clock=clock, command_broker=broker)
    service.tick()
    return service, ha, clock


def test_manual_temperature_in_observe_has_durable_intent_and_observed_confirmation(runtime):
    service, ha, _ = runtime

    def durable_before_post():
        intent = load_intent(manual_path(service.config.state_path), "binding")
        assert intent.value == 20
        assert intent.outstanding
        saved = load_state(service.config.state_path, "binding")
        assert saved.phase == "idle"
        assert saved.hold_until > service.clock()
        status = service.controls.status()
        assert status["pending"]
        assert status["outcome"] == "pending"
        assert not status["available"]

    ha.before_post = durable_before_post
    assert service.controls.submit_temperature(20)
    result = service.tick()
    assert ha.calls == [("temperature", 20)]
    assert result["control"]["outcome"] == "pending"
    assert result["climate"]["target_c"] == 18
    assert not result["control"]["available"]
    assert service.tick()["control"]["outcome"] == "confirmed"
    assert len(ha.calls) == 1


def test_disabled_manual_controls_keep_observe_read_only(runtime):
    service, ha, _ = runtime
    service.config = replace(service.config, device=DeviceConfig())
    assert service.controls.submit_temperature(20)
    assert service.tick()["decision"]["reason"] == "controls_disabled"
    assert not ha.calls


@pytest.mark.parametrize("failing_save", ["save_state", "save_intent"])
def test_no_post_when_either_journal_cannot_be_persisted(runtime, monkeypatch, failing_save):
    service, ha, _ = runtime
    assert service.controls.submit_temperature(20)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(f"inverter_climate.service.{failing_save}", fail)
    with pytest.raises(OSError):
        service.tick()
    assert not ha.calls


@pytest.mark.parametrize("failing_save", ["save_intent", "save_state"])
def test_failed_manual_persistence_retains_previous_durable_boost_ownership(
    runtime,
    monkeypatch,
    failing_save,
):
    service, ha, clock = runtime
    service.state = State(
        phase="boosted",
        baseline_c=17.5,
        boosted_c=18,
        since=clock() - 100,
        last_command=clock() - 100,
        observed_mode="heat",
        observed_target_c=18,
        observed_preset="none",
    )
    save_state(service.config.state_path, "binding", service.state)
    assert service.controls.submit_mode("off")

    def fail(*_args):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(f"inverter_climate.service.{failing_save}", fail)
        with pytest.raises(OSError):
            service.tick()
    restarted = Service(service.config, ha, Energy(), "binding", clock=clock)
    assert restarted.state.phase == "boosted"
    assert restarted.state.baseline_c == 17.5
    assert restarted.state.boosted_c == 18
    assert restarted.manual_outstanding == (failing_save == "save_state")
    if failing_save == "save_state":
        assert restarted.tick()["decision"]["reason"] == "manual_command_unconfirmed_no_retry"
        assert restarted.state.phase == "boosted"
    else:
        assert not manual_path(service.config.state_path).exists()
    assert not ha.calls


def test_http_timeout_and_restart_never_replay_even_when_controls_disabled(runtime):
    service, ha, clock = runtime
    ha.fail_write = True
    assert service.controls.submit_temperature(20)
    assert service.tick()["control"]["outcome"] == "unconfirmed"
    config = replace(service.config, mode="active", device=DeviceConfig())
    restarted = Service(config, ha, Energy(), "binding", clock=clock)
    clock.now += 10000
    assert restarted.tick()["decision"]["reason"] == "manual_command_unconfirmed_no_retry"
    assert len(ha.calls) == 1
    assert not restarted.controls.submit_temperature(21)
    ha.target = 20
    assert restarted.tick()["control"]["outcome"] == "confirmed"
    assert len(ha.calls) == 1


def test_successful_post_waits_for_observation_and_timeout_does_not_resend(runtime):
    service, ha, clock = runtime
    ha.apply = False
    assert service.controls.submit_temperature(20)
    service.tick()
    assert not service.controls.submit_mode("off")
    clock.now += 121
    assert service.tick()["control"]["outcome"] == "unconfirmed"
    service.tick()
    assert len(ha.calls) == 1


def test_confirmation_polling_is_bounded_and_does_not_retry(runtime):
    service, ha, clock = runtime
    assert service.next_poll_seconds() == 30
    ha.apply = False
    assert service.controls.submit_temperature(20)
    assert service.next_poll_seconds() == 0
    service.tick()
    assert service.next_poll_seconds() == 5
    clock.now += 5
    service.tick()
    assert service.next_poll_seconds() == 5
    assert len(ha.calls) == 1
    clock.now += 120
    service.tick()
    assert service.next_poll_seconds() == 30
    assert len(ha.calls) == 1
    ha.target = 20
    service.tick()
    assert service.next_poll_seconds() == 30


def test_immediate_http_uncertainty_uses_normal_polling(runtime):
    service, ha, _ = runtime
    ha.fail_write = True
    service.controls.submit_mode("off")
    service.tick()
    assert service.next_poll_seconds() == 30


def test_external_override_resolves_uncertain_intent_and_holds_automation(runtime):
    service, ha, clock = runtime
    ha.fail_write = True
    service.controls.submit_temperature(20)
    service.tick()
    ha.target = 21
    result = service.tick()
    assert result["control"]["outcome"] == "rejected"
    assert result["decision"]["reason"] == "external_change_respected"
    assert service.state.hold_until == clock() + service.config.policy.manual_hold_seconds
    assert len(ha.calls) == 1


def test_late_mode_confirmation_off_without_setpoint_and_heat_reenable(runtime):
    service, ha, _ = runtime
    assert service.controls.submit_mode("off")
    service.tick()
    result = service.tick()
    assert result["control"]["outcome"] == "confirmed"
    assert result["climate"]["target_c"] is None
    assert result["control"]["can_set_mode"]
    assert not result["control"]["can_set_temperature"]
    assert service.controls.submit_mode("heat")
    service.tick()
    assert service.tick()["control"]["outcome"] == "confirmed"
    assert ha.calls == [("mode", "off"), ("mode", "heat")]


@pytest.mark.parametrize("phase", ["pending_boost", "pending_restore"])
def test_uncertain_automatic_command_rejects_already_queued_manual_work(runtime, phase):
    service, ha, clock = runtime
    assert service.controls.submit_mode("off")
    service.state = State(
        phase=phase,
        baseline_c=17,
        boosted_c=18.5,
        since=clock(),
        last_command=clock(),
        observed_target_c=18,
    )
    ha.target = 17 if phase == "pending_boost" else 18.5
    result = service.tick()
    assert result["control"]["reason"] == "automatic_command_unconfirmed"
    assert not result["control"]["available"]
    assert not service.controls.submit_mode("off")
    assert not ha.calls


def test_manual_command_relinquishes_confirmed_boost_never_restores_old_baseline(runtime):
    service, ha, clock = runtime
    service.state = State(
        phase="boosted", baseline_c=17.5, boosted_c=18, since=clock(), last_command=clock()
    )
    assert service.controls.submit_temperature(20)
    service.tick()
    assert service.state.phase == "idle"
    assert service.state.baseline_c is None
    service.tick()
    service.config = replace(service.config, mode="active")
    service.tick()
    assert ha.calls == [("temperature", 20)]


@pytest.mark.parametrize("change", ["target", "mode", "preset", "capability", "offline"])
def test_fresh_ha_preflight_rejects_changed_or_unavailable_state(runtime, change):
    service, ha, _ = runtime
    assert service.controls.submit_temperature(20)
    if change == "target":
        ha.target = 19
    elif change == "mode":
        ha.mode = "off"
    elif change == "preset":
        ha.preset = "eco"
    elif change == "capability":
        ha.modes = ["off"]
    else:
        ha.fail = True
    service.tick()
    assert not ha.calls
    assert service.controls.take() is None


def test_queued_request_expires_instead_of_running_after_pause(runtime):
    service, ha, clock = runtime
    assert service.controls.submit_temperature(20)
    clock.now += 60
    assert service.tick()["decision"]["reason"] == "manual_request_expired_or_unavailable"
    assert not ha.calls


def test_fahrenheit_request_is_converted_to_native_unit(runtime):
    service, ha, _ = runtime
    ha.unit, ha.target = "°F", 64
    service.tick()
    assert service.controls.submit_temperature(20)
    service.tick()
    assert ha.calls == [("temperature", 68)]


def test_mode_already_observed_needs_no_post_but_still_holds_automation(runtime):
    service, ha, clock = runtime
    assert service.controls.submit_mode("heat")
    assert service.tick()["control"]["outcome"] == "confirmed"
    assert service.state.hold_until > clock()
    assert not ha.calls


def test_slow_journal_cannot_execute_an_expired_request(runtime, monkeypatch):
    service, ha, clock = runtime
    original = save_intent

    def slow_save(*args):
        original(*args)
        clock.now += 61

    monkeypatch.setattr("inverter_climate.service.save_intent", slow_save)
    assert service.controls.submit_temperature(20)
    assert service.tick()["control"]["outcome"] == "rejected"
    assert not ha.calls


@pytest.mark.parametrize("restore_fails", [False, True])
def test_expiry_restores_previous_boost_before_rejecting_known_unsent_command(
    runtime,
    monkeypatch,
    restore_fails,
):
    service, ha, clock = runtime
    ha.target = 18.5
    service.state = State(
        phase="boosted",
        baseline_c=18,
        boosted_c=18.5,
        since=clock() - 100,
        last_command=clock() - 100,
        observed_mode="heat",
        observed_target_c=18.5,
        observed_preset="none",
    )
    service.tick()
    assert service.controls.submit_mode("off")
    saves = 0

    def slow_save(*args):
        save_intent(*args)
        clock.now += 61

    def maybe_fail_restore(*args):
        nonlocal saves
        saves += 1
        if restore_fails and saves == 2:
            raise OSError("restore could not persist")
        save_state(*args)

    monkeypatch.setattr("inverter_climate.service.save_intent", slow_save)
    monkeypatch.setattr("inverter_climate.service.save_state", maybe_fail_restore)
    if restore_fails:
        with pytest.raises(OSError):
            service.tick()
        assert load_intent(manual_path(service.config.state_path), "binding").outstanding
        restarted = Service(service.config, ha, Energy(), "binding", clock=clock)
        assert restarted.tick()["decision"]["reason"] == "manual_command_unconfirmed_no_retry"
    else:
        assert service.tick()["control"]["outcome"] == "rejected"
        saved = load_state(service.config.state_path, "binding")
        assert saved.phase == "boosted"
        assert saved.baseline_c == 18
        assert saved.boosted_c == 18.5
        assert saved.last_command == 9900
    assert not ha.calls


def test_energy_delay_does_not_refresh_old_ha_capabilities(runtime):
    service, _, clock = runtime

    def delayed():
        clock.now += 61
        raise IntegrationError("offline")

    service.gateway.get_energy = delayed
    assert not service.tick()["control"]["available"]
    assert not service.controls.submit_mode("off")


def test_request_during_automatic_preflight_has_manual_priority(runtime):
    service, ha, clock = runtime
    service.config = replace(service.config, mode="active")
    service.state.surplus_since = clock() - 200
    service.state.last_tick = clock() - 30
    count = 0

    def get_energy():
        nonlocal count
        count += 1
        if count == 2:
            assert service.controls.submit_mode("off")
        return {
            "schema_version": 1,
            "source_type": "venus_dbus",
            "source_connected": True,
            "generated_at": clock(),
            "metrics": {
                name: {
                    "value": value,
                    "unit": unit,
                    "status": "fresh",
                    "age_seconds": 0,
                    "sources": ["system/0/example"],
                }
                for name, value, unit in (
                    ("battery_soc", 95, "%"),
                    ("solar_power", 2000, "W"),
                    ("grid_power", -700, "W"),
                    ("battery_power", 0, "W"),
                )
            },
        }

    service.gateway.get_energy = get_energy
    assert service.tick()["decision"]["reason"] == "manual_request_queued"
    assert service.state.phase == "idle"
    assert not ha.calls
    service.tick()
    assert ha.calls == [("mode", "off")]


def test_release_exit_does_not_report_success_for_unresolved_manual_command(runtime, monkeypatch):
    from inverter_climate import service as module

    service, ha, _ = runtime
    ha.apply = False
    assert service.controls.submit_temperature(20)
    service.tick()
    binding = identity("http://ha.example", service.config.entity_id)
    save_state(service.config.state_path, binding, service.state)
    save_intent(manual_path(service.config.state_path), binding, service._intent)
    monkeypatch.setattr(module, "required_env", lambda _key: "http://ha.example")
    monkeypatch.setattr(module.Config, "load", lambda _path: service.config)
    monkeypatch.setattr(module, "HomeAssistantClient", lambda *_args: ha)
    monkeypatch.setattr(module, "make_energy_client", lambda _config: service.gateway)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(ha, "close", lambda: None, raising=False)
    monkeypatch.setattr(service.gateway, "close", lambda: None, raising=False)
    monkeypatch.setattr(module.sys, "argv", ["inverter-climate", "--release"])
    assert module.main() == 1
    assert len(ha.calls) == 1
