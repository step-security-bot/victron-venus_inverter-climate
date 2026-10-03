"""Publishing lifecycle stays separate from thermostat commands and release."""

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from inverter_climate.config import Config, DeviceConfig, EnergyConfig, Policy
from inverter_climate.service import main, make_device_publisher


@pytest.fixture
def config(tmp_path):
    return Config(
        "climate.example",
        "observe",
        30,
        tmp_path / "journal.json",
        tmp_path / "status.json",
        Policy(),
        EnergyConfig("venus", ("Dc/Pv/Power",), ("L1",)),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": "true"},
        {"enabled": 1},
        {"device_instance": True},
        {"device_instance": -1},
        {"device_instance": 256},
        {"custom_name": ""},
        {"custom_name": " "},
        {"custom_name": "x" * 65},
        {"custom_name": "device\nname"},
        {"custom_name": "device\u0085name"},
        {"stale_seconds": True},
        {"stale_seconds": 9},
        {"stale_seconds": 901},
        {"stale_seconds": float("nan")},
    ],
)
def test_device_config_rejects_invalid_identity_or_staleness(values):
    with pytest.raises(ValueError):
        DeviceConfig(**values)


def test_native_device_setting_defaults_and_strict_parsing(tmp_path):
    path = tmp_path / "config.toml"
    base = """
[service]
entity_id = "climate.example"
[energy]
backend = "venus"
solar_paths = ["Dc/Pv/Power"]
grid_phases = ["L1"]
"""
    path.write_text(base)
    assert Config.load(str(path)).device == DeviceConfig()
    for extra in ("stale_seconds = 59", "enabeld = false", 'enabled = "false"'):
        path.write_text(base + "\n[device]\n" + extra)
        with pytest.raises(ValueError):
            Config.load(str(path))
    path.write_text(base + "\n[device]\nenabled = false\n")
    assert not Config.load(str(path)).device.enabled


def test_gateway_and_explicit_disabled_device_need_no_publisher(config, monkeypatch):
    monkeypatch.setitem(sys.modules, "inverter_climate.dbus_device", None)
    assert make_device_publisher(replace(config, energy=EnergyConfig()), "binding") is None
    assert make_device_publisher(replace(config, device=DeviceConfig(enabled=False)), "b") is None


def test_native_factory_passes_only_hashed_identity_and_metadata(config, monkeypatch):
    values = {}

    def publisher(**kwargs):
        values.update(kwargs)
        return values

    monkeypatch.setitem(
        sys.modules,
        "inverter_climate.dbus_device",
        SimpleNamespace(DbusDevicePublisher=publisher),
    )
    assert make_device_publisher(config, "hashed-binding") is values
    assert values["identity"] == "hashed-binding"
    assert values["device_instance"] == 80
    assert values["custom_name"] == "Inverter Climate"
    assert values["stale_seconds"] == 120
    assert "climate.example" not in str(values)


@pytest.mark.parametrize("command", [[], ["--once"], ["--release"]])
def test_daemon_publishes_but_foreground_probes_and_release_do_not(config, monkeypatch, command):
    from inverter_climate import service as module

    events = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, _):
            self.stopped = True

        def wake(self):
            pass

    class Publisher:
        def start(self):
            events.append("start")

        def check_health(self):
            events.append("health")

        def publish(self, result):
            assert result["mode"] == "observe"
            events.append("publish")

        def close(self):
            events.append("close")

    result = {
        "generated_at": 1234,
        "mode": "observe",
        "phase": "idle",
        "decision": {"action": "wait", "reason": "no_preheat_needed"},
        "errors": [],
    }
    stop = Stop()
    fake_service = SimpleNamespace(
        state=SimpleNamespace(phase="idle"),
        tick=lambda **_: result,
        controls=stop,
        manual_outstanding=False,
        next_poll_seconds=lambda: config.poll_seconds,
    )
    monkeypatch.setenv("HA_BASE_URL", "http://example.test")
    monkeypatch.setenv("HA_TOKEN", "example-token")
    monkeypatch.setattr(sys, "argv", ["inverter-climate", *command])
    monkeypatch.setattr(module.Config, "load", lambda _: config)
    monkeypatch.setattr(module, "Service", lambda *args, **kwargs: fake_service)
    monkeypatch.setattr(module.threading, "Event", lambda: stop)
    monkeypatch.setattr(module.signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        module, "HomeAssistantClient", lambda *_: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(module, "make_energy_client", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "make_device_publisher", lambda *_: Publisher())
    assert main() == 0
    assert events == ([] if command else ["start", "health", "publish", "close"])
