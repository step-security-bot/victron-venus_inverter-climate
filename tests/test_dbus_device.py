"""Temperature telemetry and optional asynchronous controls stay truthful."""

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from inverter_climate.clients import IntegrationError
from inverter_climate.dbus_device import (
    DbusDevicePublisher,
    _command_item_type,
    _Device,
    _firmware_version,
    _snapshot,
    _TemperatureUnit,
)


def fake_method(*_args, **_kwargs):
    return lambda function: function


FakeCommandItem = _command_item_type(object, fake_method, lambda value: value)


class FakeUInt32(int):
    pass


def status():
    return {
        "generated_at": 1000,
        "mode": "observe",
        "phase": "idle",
        "climate": {"current_c": 21.5, "target_c": 17, "mode": "heat", "action": "idle"},
        "decision": {"action": "wait", "reason": "no_preheat_needed", "target_c": None},
        "estimated_heating_power_w": 500,
        "errors": [],
        "entity_id": "climate.private_entity",
        "url": "http://private.example",
    }


class FakeService:
    def __init__(self, name, *, bus, register):
        assert register is False
        self.name = name
        self.bus = bus
        self.paths = {}
        self.options = {}
        self.registered = False
        self.closed = False
        self.batches = []
        self.failure = None
        self.threads = [threading.get_ident()]

    def add_path(self, path, value, **options):
        assert not self.registered
        self.paths[path] = value
        self.options[path] = options

    def set_value(self, path, value):
        options = self.options[path]
        if not options.get("writeable"):
            return 1
        item = options["itemtype"]()
        item._writeable = True
        item._path = path
        item._value = self.paths[path]
        item._onchangecallback = options["onchangecallback"]
        result = item.SetValue(value)
        # An ordinary velib SetValue would mutate the value here, so also check
        # the custom item itself has kept the last observed value untouched.
        assert item._value == self.paths[path]
        return result

    def register(self):
        assert "/Temperature" in self.paths
        assert "/CustomName" in self.paths
        assert "/DeviceInstance" in self.paths
        self.registered = True

    def __setitem__(self, path, value):
        self.threads.append(threading.get_ident())
        if self.failure:
            raise self.failure
        self.paths[path] = value

    def __enter__(self):
        self.before = dict(self.paths)
        return self

    def __exit__(self, *_args):
        self.batches.append(
            {key: value for key, value in self.paths.items() if self.before[key] != value}
        )

    def __del__(self):
        self.closed = True


class FakeSettings(dict):
    def __init__(self, _bus, supported, callback, *, timeout, persisted=None):
        assert timeout == 5
        self.supported = supported
        self.callback = callback
        self.writes = []
        super().__init__({key: value[1] for key, value in supported.items()})
        self.update(persisted or {})

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        old = self.get(key)
        super().__setitem__(key, value)
        self.callback(key, old, value)


class FakeBus:
    def __init__(self):
        self.closed = False

    def call_on_disconnection(self, callback):
        self.disconnected = callback

    def close(self):
        self.closed = True


class FakeGLib:
    """Dispatch callbacks on the real worker thread without one-second waits."""

    def __init__(self):
        self.queue = queue.Queue()
        self.running = threading.Event()
        self.quit_requested = False
        self.removed = []

    def MainLoop(self):
        return self

    def timeout_add(self, interval, callback):
        assert interval == 1000
        self.callback = callback
        return 42

    def source_remove(self, source):
        self.removed.append(source)

    def run(self):
        self.running.set()
        while not self.quit_requested:
            try:
                callback, finished = self.queue.get(timeout=0.02)
            except queue.Empty:
                # Also allow close() to finish without the test pushing a tick.
                self.callback()
                continue
            try:
                callback()
            finally:
                finished.set()

    def quit(self):
        self.quit_requested = True

    def invoke(self, callback=None):
        assert self.running.wait(1)
        finished = threading.Event()
        self.queue.put((callback or self.callback, finished))
        assert finished.wait(1)


class Runtime:
    def __init__(self, persisted=None):
        self.glib = FakeGLib()
        self.bus = FakeBus()
        self.persisted = persisted
        self.services = []
        self.settings = []
        self.now = 100
        self.unit_setting = None

    def unit_factory(self, _bus, callback):
        self.unit_setting = SimpleNamespace(value="celsius", callback=callback, closed=False)

        def close():
            self.unit_setting.closed = True

        self.unit_setting.close = close
        return self.unit_setting

    def service_factory(self, *args, **kwargs):
        service = FakeService(*args, **kwargs)
        self.services.append(service)
        return service

    def settings_factory(self, *args, **kwargs):
        settings = FakeSettings(*args, **kwargs, persisted=self.persisted)
        self.settings.append(settings)
        return settings

    def __call__(self):
        return (
            self.glib,
            self.bus,
            self.service_factory,
            self.settings_factory,
            FakeUInt32,
            FakeCommandItem,
            self.unit_factory,
        )

    def publisher(self, **kwargs):
        return DbusDevicePublisher(
            "http://private.example/climate.private_entity",
            runtime_factory=self,
            clock=lambda: self.now,
            **kwargs,
        )


@pytest.fixture
def running():
    runtime = Runtime()
    publisher = runtime.publisher()
    publisher.start()
    yield runtime, publisher
    publisher.close()


def test_registers_supported_complete_temperature_device_and_persistent_identity():
    runtime = Runtime(persisted={"instance": "temperature:82", "name": "Living room"})
    publisher = runtime.publisher()
    device = _Device(publisher, runtime.bus, runtime.service_factory, runtime.settings_factory)
    service = runtime.services[0]
    assert service.registered
    assert service.name.startswith("com.victronenergy.temperature.inverter_climate_")
    assert service.paths["/ProductName"] == "Inverter Climate"
    assert service.paths["/CustomName"] == "Living room"
    assert service.paths["/DeviceInstance"] == 82
    assert service.paths["/TemperatureType"] == 3
    assert service.paths["/Connected"] == 0
    assert service.paths["/Temperature"] is None
    assert service.paths["/ProductId"] == 0xFFFF
    for path in (
        "/Serial",
        "/Mgmt/ProcessName",
        "/Mgmt/ProcessVersion",
        "/Mgmt/Connection",
        "/FirmwareVersion",
    ):
        assert service.paths[path]
    assert "private" not in repr(service.paths)
    assert "private" not in repr(runtime.settings[0].supported)
    assert runtime.settings[0].writes == []
    device.close()
    device.close()
    assert service.closed


def test_identity_is_stable_and_not_linked_to_configurable_instance():
    first = DbusDevicePublisher("private identity", 80)
    second = DbusDevicePublisher("private identity", 81)
    third = DbusDevicePublisher("different identity", 80)
    assert first.service_name == second.service_name
    assert first.service_name != third.service_name
    assert "private identity" not in first.device_id


def test_only_validated_custom_name_is_writable_and_persistent():
    runtime = Runtime()
    publisher = runtime.publisher()
    device = _Device(publisher, runtime.bus, runtime.service_factory, runtime.settings_factory)
    service = runtime.services[0]
    writable = {path for path, options in service.options.items() if options.get("writeable")}
    assert writable == {"/CustomName"}
    rename = service.options["/CustomName"]["onchangecallback"]
    assert rename("/CustomName", "Upstairs") is True
    assert runtime.settings[0].writes == [("name", "Upstairs")]
    assert service.paths["/CustomName"] == "Upstairs"
    for invalid in (None, 1, "", " ", "x" * 65, "name\n", "secret\x00"):
        assert rename("/CustomName", invalid) is False
    assert len(runtime.settings[0].writes) == 1
    device.close()


def test_device_instance_change_reannounces_complete_service_for_consumers():
    runtime = Runtime()
    publisher = runtime.publisher()
    device = _Device(publisher, runtime.bus, runtime.service_factory, runtime.settings_factory)
    settings = runtime.settings[0]
    settings["instance"] = "temperature:81"
    assert runtime.services[0].closed
    assert runtime.services[-1].paths["/DeviceInstance"] == 81
    for value in ("acload:20", "temperature:256", "temperature:-1", "temperature:01", None):
        settings["instance"] = value
        assert runtime.services[-1].paths["/DeviceInstance"] == 81
    assert len(runtime.services) == 2
    device.close()


@pytest.mark.parametrize("persisted", [{"instance": "acload:80"}, {"name": "\n"}])
def test_invalid_persisted_settings_fail_before_service_registration(persisted):
    runtime = Runtime(persisted)
    with pytest.raises(IntegrationError, match="settings"):
        _Device(runtime.publisher(), runtime.bus, runtime.service_factory, runtime.settings_factory)
    assert runtime.services == []


def test_snapshot_is_batched_on_worker_and_estimated_power_is_never_measured_power(running):
    runtime, publisher = running
    sample = status()
    publisher.publish(sample)
    sample["climate"]["current_c"] = 99
    runtime.glib.invoke()
    service = runtime.services[0]
    assert service.paths["/Temperature"] == 21.5
    assert service.paths["/Climate/TargetTemperature"] == 17
    assert service.paths["/Climate/EstimatedHeatingPower"] == 500
    assert service.paths["/Climate/ServiceMode"] == "observe"
    assert service.paths["/Climate/HvacMode"] == "heat"
    assert service.paths["/Climate/IntegrationHealthy"] == 1
    assert len(service.batches) == 1
    assert all(not path.startswith(("/Ac", "/Dc", "/Energy")) for path in service.paths)
    assert "private" not in repr(service.paths)
    assert len(set(service.threads)) == 1
    assert service.threads[0] != threading.get_ident()


def test_metadata_raw_values_and_text_suit_both_victron_gui_generations(running):
    runtime, publisher = running
    publisher.publish(status())
    runtime.glib.invoke()
    service = runtime.services[0]

    def text(path, value=None):
        return service.options[path]["gettextcallback"](
            path, service.paths[path] if value is None else value
        )

    assert type(service.paths["/ProductId"]) is FakeUInt32
    assert text("/ProductId") == "0xffff"
    # GUI v2 formats integers as hex/BCD (3000 becomes vB.B8), but passes string
    # versions through unchanged. GUI v1 uses the same human-readable GetText.
    assert type(service.paths["/FirmwareVersion"]) is str
    assert service.paths["/FirmwareVersion"] == publisher.firmware_version
    assert text("/FirmwareVersion") == publisher.firmware_version
    assert service.paths["/HardwareVersion"] == "Virtual"
    assert text("/HardwareVersion") == "Virtual"
    assert text("/Temperature") == "21.5 °C"
    assert text("/Climate/TargetTemperature") == "17.0 °C"
    assert text("/Climate/EstimatedHeatingPower") == "500 W"
    assert service.options["/Temperature"]["gettextcallback"]("/Temperature", None) == ""


def test_firmware_version_validates_and_retains_canonical_release_identity():
    versions = ["0.0.0", "0.2.0", "0.2.1", "0.3.0", "0.999.999", "1.0.0", "999.999.999"]
    assert [_firmware_version(version) for version in versions] == versions
    for invalid in ("0.2", "01.2.0", "1.1000.0", None, "secret"):
        with pytest.raises(ValueError):
            _firmware_version(invalid)


@pytest.mark.parametrize(
    "version",
    [
        "0.3.0",
        "0.3.0b1",
        "0.3.0.dev1000001",
        "0.3.0rc1",
        "0.3.0a1",
        "0.3.0.dev18446744073709551614",
    ],
)
def test_release_runtime_versions_preserve_full_raw_value_and_text(version):
    runtime = Runtime()
    publisher = runtime.publisher(firmware_version=version)
    publisher.start()
    try:
        service = runtime.services[0]
        assert service.paths["/FirmwareVersion"] == version
        assert (
            service.options["/FirmwareVersion"]["gettextcallback"](
                "/FirmwareVersion", service.paths["/FirmwareVersion"]
            )
            == version
        )
        publisher.check_health()
    finally:
        publisher.close()


@pytest.mark.parametrize(
    "version",
    [
        "0.3.0b",
        "0.3.0dev1",
        "0.3.0.dev-1",
        "0.3.0b01",
        "0.3.0rc1junk",
        "0.3.0rc1\n",
        "v0.3.0",
        "0.3.0+private",
        "0.3.0.post1",
        "0.3.0-b1",
        "0.3.0.dev18446744073709551615",
        "0.3.0.dev" + "1" * 100,
    ],
)
def test_malformed_or_unsupported_release_suffix_fails_before_startup(version):
    with pytest.raises(ValueError, match="PEP 440"):
        DbusDevicePublisher("identity", firmware_version=version)


def test_latest_snapshot_replaces_pending_work_instead_of_queuing(running):
    runtime, publisher = running
    # Block dispatch while filling the handoff to make the test deterministic.
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(1)

    finished = threading.Event()
    runtime.glib.queue.put((block, finished))
    assert entered.wait(1)
    for temperature in (21, 22, 23):
        sample = status()
        sample["climate"]["current_c"] = temperature
        publisher.publish(sample)
    release.set()
    assert finished.wait(1)
    runtime.glib.invoke()
    assert runtime.services[0].paths["/Temperature"] == 23
    assert len(runtime.services[0].batches) == 1


def test_stale_measurements_are_invalidated_once_and_recover(running):
    runtime, publisher = running
    publisher.publish(status())
    runtime.glib.invoke()
    runtime.now += 120
    runtime.glib.invoke()
    service = runtime.services[0]
    assert service.paths["/Connected"] == 0
    assert service.paths["/Temperature"] is None
    assert service.paths["/Climate/TargetTemperature"] is None
    assert service.paths["/Climate/IntegrationHealthy"] == 0
    assert service.closed
    batches = len(service.batches)
    runtime.glib.invoke()
    assert len(service.batches) == batches
    publisher.publish(status())
    runtime.glib.invoke()
    assert len(runtime.services) == 2
    assert runtime.services[-1].paths["/Connected"] == 1
    assert runtime.services[-1].name == service.name


def test_missing_climate_invalidates_temperature_but_retains_policy_reason(running):
    runtime, publisher = running
    publisher.publish(status())
    runtime.glib.invoke()
    sample = status()
    sample["climate"] = None
    sample["errors"] = ["thermostat_read_failed"]
    sample["decision"]["reason"] = "thermostat_unavailable"
    publisher.publish(sample)
    runtime.glib.invoke()
    service = runtime.services[0]
    assert service.paths["/Connected"] == 0
    assert service.paths["/Temperature"] is None
    assert service.paths["/Climate/DecisionReason"] == "thermostat_unavailable"


def test_continuing_failed_polls_withdraw_only_after_last_good_observation_expires(running):
    runtime, publisher = running
    publisher.publish(status())
    runtime.glib.invoke()
    initial = runtime.services[0]
    sample = status()
    sample["climate"] = None
    for offset in (30, 60, 90):
        runtime.now = 100 + offset
        publisher.publish(sample)
        runtime.glib.invoke()
        assert not initial.closed
        assert initial.paths["/Connected"] == 0
    runtime.now = 220
    publisher.publish(sample)
    runtime.glib.invoke()
    assert initial.closed
    publisher.check_health()  # the HA observer can still recover
    runtime.now = 250
    publisher.publish(sample)
    runtime.glib.invoke()
    assert len(runtime.services) == 1
    publisher.publish(status())
    runtime.glib.invoke()
    assert len(runtime.services) == 2
    assert runtime.services[-1].paths["/Temperature"] == 21.5


def test_failed_energy_keeps_actual_room_measurement_but_marks_unhealthy():
    sample = status()
    sample["errors"] = ["energy_read_failed"]
    values = _snapshot(sample)
    assert values["/Connected"] == 1
    assert values["/Temperature"] == 21.5
    assert values["/Climate/IntegrationHealthy"] == 0


@pytest.mark.parametrize("value", [None, True, "21", float("nan"), float("inf"), 101, 10**400])
@pytest.mark.parametrize("field", ["current_c", "target_c"])
def test_invalid_temperatures_fail_closed(field, value):
    sample = status()
    sample["climate"][field] = value
    values = _snapshot(sample)
    if field == "current_c":
        assert values["/Connected"] == 0
        assert values["/Temperature"] is None
    else:
        assert values["/Connected"] == 1
        assert values["/Temperature"] == 21.5
        assert values["/Climate/TargetTemperature"] is None


def test_snapshot_filters_unexpected_text_and_arbitrary_error_payloads():
    sample = status()
    sample["climate"]["mode"] = "https://private.example/secret"
    sample["decision"]["reason"] = "private token\n"
    sample["errors"] = ["secret response body"]
    values = _snapshot(sample)
    assert values["/Climate/HvacMode"] == "unknown"
    assert values["/Climate/DecisionReason"] == "unknown"
    assert "secret" not in repr(values)


def test_worker_failure_is_visible_to_publish_and_health_check(running):
    runtime, publisher = running
    runtime.services[0].failure = RuntimeError("private token")
    publisher.publish(status())
    runtime.glib.invoke()
    for operation in (publisher.check_health, lambda: publisher.publish(status())):
        with pytest.raises(IntegrationError, match="unavailable") as caught:
            operation()
        assert "private" not in str(caught.value)


def test_bus_disconnect_fails_health_instead_of_silently_losing_gui_device(running):
    runtime, publisher = running
    runtime.glib.invoke(lambda: runtime.bus.disconnected(runtime.bus))
    with pytest.raises(IntegrationError, match="unavailable"):
        publisher.check_health()


def test_startup_failure_is_sanitized_and_no_optional_null_service_is_used():
    def broken_runtime():
        raise RuntimeError("private credentials")

    publisher = DbusDevicePublisher("identity", runtime_factory=broken_runtime)
    with pytest.raises(IntegrationError, match="unavailable") as caught:
        publisher.start()
    assert "private" not in str(caught.value)
    publisher.close()


def test_native_thread_initialization_happens_on_caller_before_worker(monkeypatch):
    from inverter_climate import dbus_device as module

    runtime = Runtime()
    initialized = []
    monkeypatch.setattr(module, "_runtime", runtime)
    monkeypatch.setattr(
        module.importlib,
        "import_module",
        lambda name: SimpleNamespace(
            threads_init=lambda: initialized.append(threading.get_ident())
        ),
    )
    publisher = runtime.publisher()
    publisher.start()
    try:
        assert initialized == [threading.get_ident()]
        assert runtime.services[0].threads[0] != initialized[0]
    finally:
        publisher.close()


def test_native_runtime_uses_explicit_mainloop_without_changing_energy_bus_default(
    monkeypatch, tmp_path
):
    from inverter_climate import dbus_device as module

    (tmp_path / "vedbus.py").touch()
    monkeypatch.setattr(module, "_VELIB", tmp_path)
    monkeypatch.setattr(module.sys, "path", list(module.sys.path))
    loop = object()
    calls = []
    bus = SimpleNamespace(set_exit_on_disconnect=lambda value: calls.append(("exit", value)))

    def system_bus(**kwargs):
        calls.append(kwargs)
        return bus

    imports = {
        "dbus": SimpleNamespace(SystemBus=system_bus, UInt32=FakeUInt32),
        "dbus.mainloop.glib": SimpleNamespace(DBusGMainLoop=lambda: loop),
        "dbus.service": SimpleNamespace(method=fake_method),
        "gi.repository.GLib": object(),
        "vedbus": SimpleNamespace(
            VeDbusService=FakeService,
            VeDbusItemExport=object,
            unwrap_dbus_value=lambda value: value,
        ),
        "settingsdevice": SimpleNamespace(SettingsDevice=FakeSettings),
    }
    monkeypatch.setattr(module.importlib, "import_module", imports.__getitem__)
    result = module._runtime()
    assert result[1] is bus
    assert result[4] is FakeUInt32
    assert calls == [{"private": True, "mainloop": loop}, ("exit", False)]


def test_startup_timeout_is_bounded_and_late_runtime_does_not_register():
    runtime = Runtime()

    def delayed_runtime():
        time.sleep(0.05)
        return runtime()

    publisher = DbusDevicePublisher(
        "identity", runtime_factory=delayed_runtime, startup_timeout=0.01
    )
    with pytest.raises(IntegrationError, match="timed out"):
        publisher.start()
    assert not runtime.services
    assert runtime.bus.closed


def test_close_deregisters_device_closes_private_bus_and_cannot_restart():
    runtime = Runtime()
    publisher = runtime.publisher()
    publisher.start()
    publisher.start()
    publisher.close()
    publisher.close()
    assert runtime.services[0].closed
    assert runtime.bus.closed
    assert runtime.glib.removed == [42]
    assert not publisher._thread.is_alive()
    with pytest.raises(IntegrationError):
        publisher.start()
    with pytest.raises(IntegrationError):
        publisher.publish(status())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"identity": ""},
        {"identity": "x" * 4097},
        {"device_instance": True},
        {"device_instance": 256},
        {"device_instance": -1},
        {"custom_name": "\n"},
        {"stale_seconds": float("nan")},
        {"stale_seconds": True},
        {"stale_seconds": 901},
        {"firmware_version": ""},
        {"startup_timeout": 0},
        {"command_broker": object()},
    ],
)
def test_invalid_configuration_is_rejected_before_native_imports(kwargs):
    with pytest.raises(ValueError):
        DbusDevicePublisher(**({"identity": "identity"} | kwargs))


class FakeBroker:
    def __init__(self):
        self.requests = []
        self.accept = True
        self.error = None
        self.live_status = None

    def status(self):
        return self.live_status

    def submit_temperature(self, value):
        return self._submit("temperature", value)

    def submit_mode(self, value):
        return self._submit("mode", value)

    def _submit(self, kind, value):
        if self.error:
            raise self.error
        self.requests.append((kind, value))
        return self.accept


def controllable_status(**control):
    sample = status()
    sample["control"] = {
        "enabled": True,
        "available": True,
        "pending": False,
        "outcome": "idle",
        "reason": "ready",
        "can_set_temperature": True,
        "can_set_mode": True,
        "min_c": 10,
        "max_c": 32,
        "step_c": 0.5,
        "supported_modes": ["off", "heat"],
    } | control
    return sample


@pytest.fixture
def controls():
    runtime, broker = Runtime(), FakeBroker()
    publisher = runtime.publisher(command_broker=broker)
    publisher.start()
    yield runtime, publisher, broker
    publisher.close()


def command(runtime, kind, value):
    results = []
    path = f"/SwitchableOutput/{kind}/Dimming"
    runtime.glib.invoke(lambda: results.append(runtime.services[-1].set_value(path, value)))
    return results[0]


def test_switch_contract_uses_stock_temperature_and_dropdown_types(controls):
    runtime, publisher, _broker = controls
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    service = runtime.services[-1]
    for index, kind in ((0, 3), (1, 6)):
        prefix = f"/SwitchableOutput/{index}"
        assert service.paths[f"{prefix}/Settings/Type"] == kind
        assert service.paths[f"{prefix}/Settings/ValidTypes"] == 1 << kind
        assert service.paths[f"{prefix}/Settings/Adjustable"] == 0
        for setting in ("Group", "CustomName", "ShowUIControl"):
            assert service.paths[f"{prefix}/Settings/{setting}"] is None
            assert not service.options[f"{prefix}/Settings/{setting}"].get("writeable")
        assert service.paths[f"{prefix}/State"] is None
        assert service.paths[f"{prefix}/Status"] == 0x09
    assert service.paths["/SwitchableOutput/1/Settings/Labels"] == ["Off", "Heat"]
    assert service.paths["/SwitchableOutput/0/Measurement"] == 21.5
    assert service.paths["/SwitchableOutput/0/Dimming"] == 17
    assert service.paths["/SwitchableOutput/1/Dimming"] == 1
    assert {path for path, options in service.options.items() if options.get("writeable")} == {
        "/CustomName",
        "/SwitchableOutput/0/Dimming",
        "/SwitchableOutput/1/Dimming",
    }


def test_commands_are_disabled_at_startup_and_without_broker(controls, running):
    runtime, _publisher, broker = controls
    assert command(runtime, 0, 20) == 2
    assert command(runtime, 1, 1) == 2
    assert not broker.requests
    runtime, publisher = running
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    assert command(runtime, 0, 20) == 1
    assert runtime.services[-1].paths["/Climate/ControlEnabled"] == 0
    assert runtime.services[-1].paths["/SwitchableOutput/1/Status"] == 0x20


def test_commands_enqueue_even_equal_values_and_never_echo_requested_state(controls):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    assert command(runtime, 0, 17) == 0
    assert command(runtime, 0, 20) == 0
    assert command(runtime, 1, 0) == 0
    service = runtime.services[-1]
    assert broker.requests == [("temperature", 17), ("temperature", 20), ("mode", "off")]
    assert service.paths["/SwitchableOutput/0/Dimming"] == 17
    assert service.paths["/Climate/TargetTemperature"] == 17
    assert service.paths["/SwitchableOutput/1/Dimming"] == 1
    assert service.paths["/Climate/HvacMode"] == "heat"
    assert service.paths["/Climate/ControlPending"] == 1
    assert service.paths["/Climate/ControlOutcome"] == "queued"
    assert service.paths["/SwitchableOutput/0/Status"] == 0x09
    assert len(set(service.threads)) == 1
    observed = controllable_status(outcome="confirmed")
    observed["climate"]["target_c"] = 20
    publisher.publish(observed)
    runtime.glib.invoke()
    assert service.paths["/SwitchableOutput/0/Dimming"] == 20
    assert service.paths["/Climate/ControlPending"] == 0
    assert service.paths["/Climate/ControlOutcome"] == "confirmed"


def test_queued_requests_remain_coalescible_but_dispatched_requests_disable_controls(controls):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status(pending=True, outcome="queued"))
    runtime.glib.invoke()
    assert command(runtime, 0, 20) == 0
    publisher.publish(controllable_status(available=False, pending=True, outcome="pending"))
    runtime.glib.invoke()
    assert command(runtime, 0, 21) == 2
    assert command(runtime, 1, 0) == 2
    assert broker.requests == [("temperature", 20)]
    assert runtime.services[-1].paths["/SwitchableOutput/0/Status"] == 0x20


@pytest.mark.parametrize(
    "kind,value",
    [
        (0, True),
        (0, "20"),
        (0, None),
        (0, float("nan")),
        (0, float("inf")),
        (0, 9.99),
        (0, 32.01),
        (0, 10**400),
        (1, True),
        (1, "heat"),
        (1, -1),
        (1, 2),
        (1, 0.5),
        (1, float("nan")),
    ],
)
def test_invalid_requests_never_reach_broker(controls, kind, value):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    assert command(runtime, kind, value) == 2
    assert not broker.requests
    assert runtime.services[-1].paths["/Climate/ControlOutcome"] == "idle"


@pytest.mark.parametrize(
    "control",
    [
        {"enabled": False},
        {"enabled": 1},
        {"available": False},
        {"available": 1},
        {"can_set_temperature": False},
        {"min_c": 35},
        {"max_c": None},
        {"step_c": 0},
        {"step_c": float("nan")},
    ],
)
def test_temperature_capabilities_fail_closed(controls, control):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status(**control))
    runtime.glib.invoke()
    assert command(runtime, 0, 20) == 2
    assert not broker.requests
    assert runtime.services[-1].paths["/SwitchableOutput/0/Status"] == 0x20


@pytest.mark.parametrize("supported", [None, [], ["heat"], ["off"], "heat,off"])
def test_dropdown_does_not_offer_unsupported_mode_pair(controls, supported):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status(supported_modes=supported))
    runtime.glib.invoke()
    assert command(runtime, 1, 0) == 2
    assert not broker.requests
    assert runtime.services[-1].paths["/SwitchableOutput/1/Status"] == 0x20


def test_off_without_target_keeps_temperature_and_heat_mode_recovery(controls):
    runtime, publisher, broker = controls
    sample = controllable_status(can_set_temperature=False)
    sample["climate"].update(mode="off", target_c=None, action="off")
    publisher.publish(sample)
    runtime.glib.invoke()
    paths = runtime.services[-1].paths
    assert paths["/Connected"] == 1
    assert paths["/Temperature"] == 21.5
    assert paths["/Climate/IntegrationHealthy"] == 1
    assert paths["/Climate/TargetTemperature"] is None
    assert paths["/SwitchableOutput/0/State"] is None
    assert paths["/SwitchableOutput/0/Dimming"] is None
    assert paths["/SwitchableOutput/0/Status"] == 0x20
    assert paths["/SwitchableOutput/1/Dimming"] == 0
    assert paths["/SwitchableOutput/1/Status"] == 0x09
    assert command(runtime, 0, 20) == 2
    assert command(runtime, 1, 1) == 0
    assert broker.requests == [("mode", "heat")]
    assert paths["/SwitchableOutput/1/Dimming"] == 0


def test_expired_snapshot_rejects_write_before_timer_and_preserves_pending_unknown(controls):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status(pending=True, outcome="queued"))
    runtime.glib.invoke()
    runtime.now += 120
    assert command(runtime, 0, 20) == 2
    assert not broker.requests
    runtime.glib.invoke()
    paths = runtime.services[-1].paths
    assert paths["/Climate/ControlEnabled"] == 1
    assert paths["/Climate/ControlAvailable"] == 0
    assert paths["/Climate/ControlPending"] == 1
    assert paths["/Climate/ControlReason"] == "telemetry_stale"
    assert paths["/SwitchableOutput/0/Status"] == 0x20
    assert paths["/SwitchableOutput/1/Dimming"] is None


def test_energy_failure_does_not_disable_fresh_manual_ha_controls(controls):
    runtime, publisher, _broker = controls
    sample = controllable_status()
    sample["errors"] = ["energy_read_failed"]
    publisher.publish(sample)
    runtime.glib.invoke()
    assert runtime.services[-1].paths["/Climate/IntegrationHealthy"] == 0
    assert command(runtime, 1, 0) == 0


def test_broker_rejection_and_exception_do_not_claim_success(controls):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    broker.accept = False
    assert command(runtime, 0, 20) == 2
    broker.error = RuntimeError("private upstream response")
    assert command(runtime, 1, 0) == 2
    paths = runtime.services[-1].paths
    assert paths["/Climate/ControlOutcome"] == "idle"
    assert paths["/SwitchableOutput/0/Dimming"] == 17
    assert "private" not in repr(paths)
    publisher.check_health()


def test_publisher_preserves_fahrenheit_range_without_its_own_step_rounding(controls):
    runtime, publisher, broker = controls
    minimum, maximum, step = (50 - 32) * 5 / 9, (90 - 32) * 5 / 9, 5 / 9
    publisher.publish(controllable_status(min_c=minimum, max_c=maximum, step_c=step))
    runtime.glib.invoke()
    assert command(runtime, 0, maximum) == 0
    assert broker.requests == [("temperature", maximum)]
    assert runtime.services[-1].paths["/SwitchableOutput/0/Settings/StepSize"] == step


def test_command_item_obeys_completion_codes_without_optimistic_local_set():
    item = FakeCommandItem()
    item._path, item._value = "/command", 17
    seen = []
    item._onchangecallback = lambda path, value: seen.append((path, value)) or True
    item._writeable = False
    assert item.SetValue(17) == 1
    assert not seen
    item._writeable = True
    assert item.SetValue(17) == 0
    assert item.SetValue(20) == 0
    assert seen == [("/command", 17), ("/command", 20)]
    assert item._value == 17
    item._onchangecallback = None
    assert item.SetValue(20) == 1


def test_control_diagnostics_filter_untrusted_text():
    values = _snapshot(
        controllable_status(reason="private URL/token\n", outcome="upstream secret"),
        commands_supported=True,
    )
    assert values["/Climate/ControlReason"] == "unknown"
    assert values["/Climate/ControlOutcome"] == "idle"
    assert "secret" not in repr(values)


def test_disabled_configuration_does_not_hide_an_outstanding_manual_intent():
    values = _snapshot(
        controllable_status(enabled=False, available=False, pending=True, outcome="unconfirmed"),
        commands_supported=True,
    )
    assert values["/Climate/ControlEnabled"] == 0
    assert values["/Climate/ControlPending"] == 1
    assert values["/Climate/ControlOutcome"] == "unconfirmed"
    assert values["/SwitchableOutput/1/Status"] == 0x20


def test_accepted_request_stays_accepted_when_pending_telemetry_write_fails(controls):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    runtime.services[-1].failure = RuntimeError("broken D-Bus signal")
    assert command(runtime, 0, 20) == 0
    assert broker.requests == [("temperature", 20)]
    with pytest.raises(IntegrationError):
        publisher.check_health()


def test_live_broker_state_disables_blocked_or_expired_requests_without_refreshing_telemetry(
    controls,
):
    runtime, publisher, broker = controls
    publisher.publish(controllable_status())
    runtime.glib.invoke()
    original = dict(runtime.services[-1].paths)
    broker.live_status = controllable_status(available=False, pending=True, outcome="pending")[
        "control"
    ]
    runtime.now += 60
    runtime.glib.invoke()
    paths = runtime.services[-1].paths
    assert paths["/Climate/ControlAvailable"] == 0
    assert paths["/Climate/ControlPending"] == 1
    assert paths["/SwitchableOutput/0/Status"] == 0x20
    assert paths["/SwitchableOutput/1/Status"] == 0x20
    for path in ("/Temperature", "/Climate/LastUpdate", "/SwitchableOutput/0/Dimming"):
        assert paths[path] == original[path]
    # Control polling must not turn 120-second-old telemetry into a fresh read.
    runtime.now += 60
    runtime.glib.invoke()
    assert runtime.services[-1].closed
    assert paths["/Temperature"] is None


@pytest.mark.parametrize("initial_step", [0.5, 5 / 9])
def test_gui_unit_changes_scale_only_display_step_and_never_compound(controls, initial_step):
    runtime, publisher, broker = controls
    sample = controllable_status(step_c=initial_step)
    broker.live_status = sample["control"]
    publisher.publish(sample)
    runtime.glib.invoke()
    paths = runtime.services[-1].paths
    original = dict(paths)
    assert paths["/SwitchableOutput/0/Settings/StepSize"] == initial_step
    for unit, scale in (("fahrenheit", 1.8), ("fahrenheit", 1.8), ("celsius", 1)):
        runtime.glib.invoke(lambda unit=unit: runtime.unit_setting.callback(unit))
        assert paths["/SwitchableOutput/0/Settings/StepSize"] == pytest.approx(initial_step * scale)
        runtime.glib.invoke()
        assert paths["/SwitchableOutput/0/Settings/StepSize"] == pytest.approx(initial_step * scale)
        assert paths["/SwitchableOutput/0/Status"] == 0x09
        for path in (
            "/Temperature",
            "/Climate/LastUpdate",
            "/SwitchableOutput/0/Dimming",
            "/SwitchableOutput/0/Measurement",
            "/SwitchableOutput/0/Settings/DimmingMin",
            "/SwitchableOutput/0/Settings/DimmingMax",
        ):
            assert paths[path] == original[path]


@pytest.mark.parametrize("unit", [None, "unknown", "F", 1])
def test_missing_or_unknown_gui_unit_disables_only_temperature_control(controls, unit):
    runtime, publisher, broker = controls
    sample = controllable_status()
    broker.live_status = sample["control"]
    publisher.publish(sample)
    runtime.glib.invoke()
    runtime.glib.invoke(lambda: runtime.unit_setting.callback(unit))
    paths = runtime.services[-1].paths
    assert paths["/SwitchableOutput/0/Status"] == 0x20
    assert paths["/SwitchableOutput/1/Status"] == 0x09
    assert command(runtime, 0, 20) == 2
    assert command(runtime, 1, 0) == 0


def test_temperature_unit_subscription_is_read_only_and_recovers_settings_owner():
    owner_callbacks, imports, observed, removed = [], [], [], []
    next_value = ["celsius"]

    def subscribe(callback, **kwargs):
        assert kwargs == {
            "signal_name": "NameOwnerChanged",
            "dbus_interface": "org.freedesktop.DBus",
            "bus_name": "org.freedesktop.DBus",
            "arg0": "com.victronenergy.settings",
        }
        owner_callbacks.append(callback)
        return SimpleNamespace(remove=lambda: removed.append(True))

    class ReadOnlyItem:
        def __init__(self, bus, service, path, *, eventCallback):
            assert bus is fake_bus
            assert service == "com.victronenergy.settings"
            assert path == "/Settings/System/Units/Temperature"
            self.callback = eventCallback
            self.closed = False
            imports.append(self)

        def get_value(self):
            return next_value[0]

        def __del__(self):
            self.closed = True

    fake_bus = SimpleNamespace(add_signal_receiver=subscribe)
    unit = _TemperatureUnit(fake_bus, ReadOnlyItem, observed.append)
    assert unit.value == "celsius"
    imports[-1].callback("service", "path", {"Value": "fahrenheit"})
    assert unit.value == "fahrenheit"
    owner_callbacks[0]("name", "old", "")
    assert unit.value is None
    next_value[0] = "celsius"
    owner_callbacks[0]("name", "", "new")
    assert imports[0].closed
    assert unit.value == "celsius"
    assert observed == ["fahrenheit", None, "celsius"]
    unit.close()
    assert imports[-1].closed
    assert removed == [True]
