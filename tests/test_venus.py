"""Native Venus energy snapshots need no writes, discovery, or MQTT bridge."""

import importlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from inverter_climate.clients import IntegrationError
from inverter_climate.models import Energy, InvalidObservation
from inverter_climate.venus import VenusEnergyClient

SOLAR = ("Dc/Pv/Power", "Ac/PvOnGrid/L1/Power")
GRID = ("L1", "L2")


def measurements():
    return {
        "Dc/Battery/Soc": 95,
        "Dc/Battery/Power": 200,
        "Dc/Pv/Power": 800,
        "Ac/PvOnGrid/L1/Power": 350,
        "Ac/Grid/NumberOfPhases": 2,
        "Ac/Grid/L1/Power": -1000,
        "Ac/Grid/L2/Power": 300,
        "Ac/Grid/L3/Power": [],
    }


class FakeBus:
    def __init__(self, values=None, owners=(":1.42", ":1.42"), failure=None):
        self.values = measurements() if values is None else values
        self.owners = iter(owners)
        self.failure = failure
        self.calls = []
        self.closed = False
        self.exit_on_disconnect = None

    def call_blocking(self, service, path, interface, method, signature, args, *, timeout):
        self.calls.append((service, path, interface, method, signature, args, timeout))
        if method == "GetNameOwner":
            return next(self.owners)
        assert method == "GetValue"
        if self.failure is not None:
            raise self.failure
        return deepcopy(self.values)

    def set_exit_on_disconnect(self, value):
        self.exit_on_disconnect = value

    def close(self):
        self.closed = True


def client(bus, **kwargs):
    timestamps = iter((1000.0, 1000.2))
    defaults = {
        "solar_paths": SOLAR,
        "grid_phases": GRID,
        "bus_factory": lambda: bus,
        "clock": lambda: next(timestamps),
    }
    defaults.update(kwargs)
    return VenusEnergyClient(**defaults)


def test_one_root_read_preserves_signed_phase_sum_and_explicit_solar_topology():
    bus = FakeBus()
    raw = client(bus).get_energy()
    energy = Energy.parse(raw, now=1000.2, max_age=120)
    assert energy == Energy(soc=95, solar_w=1150, grid_w=-700, battery_w=200)
    assert raw["source_type"] == "venus_dbus"
    assert raw["source_connected"] is True
    assert raw["provenance"] == "local_service_read"
    assert "mqtt_connected" not in raw
    assert raw["generated_at"] == 1000.2
    assert raw["metrics"]["grid_power"]["age_seconds"] == pytest.approx(0.2)
    assert raw["metrics"]["solar_power"]["sources"] == [f"system/0/{path}" for path in SOLAR]
    assert [call[3] for call in bus.calls] == ["GetNameOwner", "GetValue", "GetNameOwner"]
    assert bus.calls[1] == (":1.42", "/", "com.victronenergy.BusItem", "GetValue", "", (), 5)
    assert all(call[-1] == 5 for call in bus.calls)


def test_signed_battery_and_grid_are_not_clamped_to_zero():
    values = measurements()
    values["Dc/Battery/Power"] = -501
    values["Ac/Grid/L1/Power"] = 400
    raw = client(FakeBus(values)).get_energy()
    assert raw["metrics"]["battery_power"]["value"] == -501
    assert raw["metrics"]["grid_power"]["value"] == 700


@pytest.mark.parametrize(
    "phases, actual",
    [(("L1",), 2), (("L1", "L2", "L3"), 2), (("L1", "L3"), 2), (GRID, 1), (GRID, 3)],
)
def test_partial_or_wrong_grid_phase_selection_cannot_hide_import(phases, actual):
    values = measurements()
    values["Ac/Grid/NumberOfPhases"] = actual
    with pytest.raises(IntegrationError, match="phases"):
        client(FakeBus(values), grid_phases=phases).get_energy()


@pytest.mark.parametrize("phases", [0, 4, 2.5, None, [], True, "2", float("nan")])
def test_invalid_live_phase_count_rejects_snapshot(phases):
    values = measurements()
    values["Ac/Grid/NumberOfPhases"] = phases
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


@pytest.mark.parametrize(
    "path", ["Dc/Battery/Soc", "Dc/Battery/Power", *SOLAR, "Ac/Grid/L1/Power", "Ac/Grid/L2/Power"]
)
@pytest.mark.parametrize("value", [None, [], True, "100", float("nan"), float("inf")])
def test_missing_null_and_malformed_selected_values_never_become_zero(path, value):
    values = measurements()
    values[path] = value
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


def test_missing_selected_solar_does_not_fall_back_to_an_unselected_aggregate():
    values = measurements()
    del values["Ac/PvOnGrid/L1/Power"]
    values["Ac/PvOnGrid/Total/Power"] = 350
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


@pytest.mark.parametrize("negative_path", SOLAR)
def test_negative_solar_component_cannot_be_hidden_by_another_positive_component(negative_path):
    values = measurements()
    values[negative_path] = -1
    assert sum(values[path] for path in SOLAR) > 0
    with pytest.raises(IntegrationError, match="negative solar measurement"):
        client(FakeBus(values)).get_energy()


def test_native_dbus_numeric_wrappers_are_normalized_to_plain_floats():
    dbus_int = type("Int32", (int,), {"__module__": "dbus"})
    dbus_float = type("Double", (float,), {"__module__": "dbus"})
    values = {
        path: dbus_float(value) if isinstance(value, int) else value
        for path, value in measurements().items()
    }
    values["Ac/Grid/NumberOfPhases"] = dbus_int(2)
    raw = client(FakeBus(values)).get_energy()
    assert all(type(metric["value"]) is float for metric in raw["metrics"].values())


@pytest.mark.parametrize("module", ["dbus", "dbus.types", "_dbus_bindings"])
def test_native_dbus_boolean_is_not_a_numeric_measurement(module):
    dbus_bool = type("Boolean", (int,), {"__module__": module})
    values = measurements()
    values["Dc/Battery/Soc"] = dbus_bool(1)
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


@pytest.mark.parametrize("value", [pytest.param(10**400, id="integer_overflow"), float("inf")])
def test_unrepresentable_numbers_are_sanitized_integration_errors(value):
    values = measurements()
    values["Dc/Pv/Power"] = value
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


def test_overflowing_sum_is_rejected_even_when_individual_values_are_finite():
    values = measurements()
    values["Dc/Pv/Power"] = 1e308
    values["Ac/PvOnGrid/L1/Power"] = 1e308
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


@pytest.mark.parametrize(
    "path, value", [("Dc/Battery/Soc", -1), ("Dc/Battery/Soc", 101), ("Dc/Pv/Power", -1000)]
)
def test_nonphysical_soc_or_negative_total_solar_is_rejected(path, value):
    values = measurements()
    values[path] = value
    with pytest.raises(IntegrationError):
        client(FakeBus(values)).get_energy()


@pytest.mark.parametrize(
    "solar",
    [
        (),
        "Dc/Pv/Power",
        ("/Dc/Pv/Power",),
        ("system/0/Dc/Pv/Power",),
        ("Ac/Grid/L1/Power",),
        ("Dc/Pv/Power", "Dc/Pv/Power"),
        ("Ac/PvOnGrid/Total/Power", "Ac/PvOnGrid/L1/Power"),
        ("Ac/PvOnOutput/Total/Power", "Ac/PvOnOutput/L3/Power"),
    ],
)
def test_invalid_or_overlapping_solar_selection_is_rejected_before_reading(solar):
    bus = FakeBus()
    with pytest.raises(IntegrationError):
        client(bus, solar_paths=solar)
    assert bus.calls == []


def test_explicit_total_for_one_placement_and_phases_for_another_do_not_overlap():
    values = measurements()
    values["Ac/PvOnOutput/Total/Power"] = 100
    values["Ac/PvOnGenset/L3/Power"] = 50
    raw = client(
        FakeBus(values), solar_paths=(*SOLAR, "Ac/PvOnOutput/Total/Power", "Ac/PvOnGenset/L3/Power")
    ).get_energy()
    assert raw["metrics"]["solar_power"]["value"] == 1300


@pytest.mark.parametrize("phases", [(), "L1", ("L1", "L1"), ("Total",), ("L4",), (None,)])
def test_invalid_grid_selection_is_rejected_before_reading(phases):
    bus = FakeBus()
    with pytest.raises(IntegrationError):
        client(bus, grid_phases=phases)
    assert bus.calls == []


@pytest.mark.parametrize("timeout", [0, -1, True, "5", float("nan"), float("inf")])
def test_timeout_must_be_a_finite_positive_number(timeout):
    with pytest.raises(IntegrationError, match="timeout"):
        client(FakeBus(), timeout_seconds=timeout)


def test_slow_local_read_cannot_refresh_old_observation_age():
    timestamps = iter((1000, 1130))
    raw = client(FakeBus(), clock=lambda: next(timestamps)).get_energy()
    assert raw["generated_at"] == 1130
    assert raw["metrics"]["solar_power"]["age_seconds"] == 130
    with pytest.raises(InvalidObservation):
        Energy.parse(raw, now=1130, max_age=120)


def test_clock_regression_during_read_rejects_snapshot():
    timestamps = iter((1000, 999))
    with pytest.raises(IntegrationError, match="Clock changed"):
        client(FakeBus(), clock=lambda: next(timestamps)).get_energy()


def test_owner_change_discards_snapshot_without_retrying():
    bus = FakeBus(owners=(":1.42", ":1.43"))
    with pytest.raises(IntegrationError, match="snapshot failed"):
        client(bus).get_energy()
    assert bus.closed
    assert len([call for call in bus.calls if call[3] == "GetValue"]) == 1


@pytest.mark.parametrize("owner", ["com.victronenergy.system", "", None, ":1", "private text"])
def test_invalid_owner_prevents_root_read(owner):
    bus = FakeBus(owners=(owner,))
    with pytest.raises(IntegrationError):
        client(bus).get_energy()
    assert [call[3] for call in bus.calls] == ["GetNameOwner"]


def test_read_error_is_sanitized_and_next_poll_can_reconnect():
    failed = FakeBus(failure=RuntimeError("private upstream detail"))
    recovered = FakeBus()
    buses = iter((failed, recovered))
    adapter = client(failed, bus_factory=lambda: next(buses), clock=lambda: 1000)
    with pytest.raises(IntegrationError) as caught:
        adapter.get_energy()
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert failed.closed
    assert len([call for call in failed.calls if call[3] == "GetValue"]) == 1
    assert adapter.get_energy()["metrics"]["grid_power"]["value"] == -700


@pytest.mark.parametrize("snapshot", [[], "private response", 7])
def test_root_snapshot_must_be_a_mapping(snapshot):
    with pytest.raises(IntegrationError, match="invalid energy snapshot"):
        client(FakeBus(snapshot)).get_energy()


def test_native_module_is_loaded_lazily_and_private_bus_cannot_exit_the_process(monkeypatch):
    calls = []
    bus = FakeBus()

    def system_bus(*, private):
        calls.append(private)
        return bus

    def fake_import(name):
        assert name == "dbus"
        calls.append(name)
        return SimpleNamespace(SystemBus=system_bus)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    adapter = VenusEnergyClient(solar_paths=SOLAR, grid_phases=GRID, clock=lambda: 1000)
    assert calls == []
    adapter.get_energy()
    assert calls == ["dbus", True]
    assert bus.exit_on_disconnect is False
    adapter.close()
    assert bus.closed
    adapter.close()
    with pytest.raises(IntegrationError, match="closed"):
        adapter.get_energy()


def test_missing_native_module_is_a_sanitized_read_error(monkeypatch):
    def no_dbus(_):
        raise ImportError("private path to unavailable module")

    monkeypatch.setattr(importlib, "import_module", no_dbus)
    adapter = VenusEnergyClient(solar_paths=SOLAR, grid_phases=GRID)
    with pytest.raises(IntegrationError) as caught:
        adapter.get_energy()
    assert "private" not in str(caught.value)
