"""Read explicitly selected energy measurements from the local Venus OS bus.

Each snapshot reads the system service once and checks its unique owner around
the read. Freshness describes that local service read, not the age of a physical
sensor sample. Selecting every installed solar placement is a deployment task;
this adapter does not discover sources or substitute zero for missing values.
"""

from __future__ import annotations

import importlib
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from .clients import IntegrationError

_SERVICE = "com.victronenergy.system"
_BUS_SERVICE = "org.freedesktop.DBus"
_BUS_PATH = "/org/freedesktop/DBus"
_ITEM_INTERFACE = "com.victronenergy.BusItem"
_SOLAR_PATH = re.compile(r"(?:Dc/Pv|Ac/PvOn(?:Grid|Output|Genset)/(?:L[123]|Total))/Power")
_UNIQUE_OWNER = re.compile(r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+")


def _number(value: Any) -> float:
    # dbus.Boolean derives from int, unlike Python's bool. Do not let either
    # authorize a power/SoC observation; accept native numeric D-Bus wrappers.
    value_type = type(value)
    is_dbus_boolean = value_type.__name__ == "Boolean" and (
        value_type.__module__.split(".")[0] in ("dbus", "_dbus_bindings")
    )
    if isinstance(value, bool) or is_dbus_boolean or not isinstance(value, (int, float)):
        raise IntegrationError("Venus OS returned a missing or invalid numeric measurement.")
    try:
        result = float(value)
    except OverflowError:
        raise IntegrationError("Venus OS returned an invalid numeric measurement.") from None
    if not math.isfinite(result):
        raise IntegrationError("Venus OS returned an invalid numeric measurement.")
    return result


def _system_bus():
    # Venus OS supplies dbus-python. Keep this optional adapter importable on
    # development machines and never install/replace the operating system copy.
    dbus = importlib.import_module("dbus")
    bus = dbus.SystemBus(private=True)
    bus.set_exit_on_disconnect(False)
    return bus


class VenusEnergyClient:
    def __init__(
        self,
        *,
        solar_paths: tuple[str, ...],
        grid_phases: tuple[str, ...],
        timeout_seconds: float = 5,
        bus_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if (
            not isinstance(solar_paths, tuple)
            or not solar_paths
            or any(
                not isinstance(path, str) or not _SOLAR_PATH.fullmatch(path) for path in solar_paths
            )
            or len(set(solar_paths)) != len(solar_paths)
        ):
            raise IntegrationError("Configure unique, explicit Venus OS solar paths.")
        for path in solar_paths:
            if path.endswith("/Total/Power"):
                prefix = path.removesuffix("/Total/Power") + "/L"
                if any(other.startswith(prefix) for other in solar_paths):
                    raise IntegrationError("Solar totals cannot overlap their phase readings.")
        if (
            not isinstance(grid_phases, tuple)
            or not grid_phases
            or any(phase not in ("L1", "L2", "L3") for phase in grid_phases)
            or len(set(grid_phases)) != len(grid_phases)
        ):
            raise IntegrationError("Configure unique, explicit Venus OS grid phases.")
        try:
            timeout = _number(timeout_seconds)
        except IntegrationError:
            raise IntegrationError("Venus OS timeout must be finite and positive.") from None
        if timeout <= 0:
            raise IntegrationError("Venus OS timeout must be finite and positive.")
        self._solar_paths = solar_paths
        self._grid_phases = grid_phases
        self._timeout = timeout
        self._bus_factory = bus_factory if bus_factory is not None else _system_bus
        self._clock = clock
        self._bus = None
        self._closed = False

    def _discard_bus(self):
        bus, self._bus = self._bus, None
        if bus is not None:
            try:
                bus.close()
            except Exception:
                # Closing a failed connection must not conceal the read error.
                pass

    def _owner(self) -> str:
        owner = self._bus.call_blocking(
            _BUS_SERVICE,
            _BUS_PATH,
            _BUS_SERVICE,
            "GetNameOwner",
            "s",
            (_SERVICE,),
            timeout=self._timeout,
        )
        if not isinstance(owner, str) or not _UNIQUE_OWNER.fullmatch(owner):
            raise IntegrationError("Venus OS system service has no valid owner.")
        return str(owner)

    def _snapshot(self) -> Mapping:
        if self._closed:
            raise IntegrationError("Venus OS energy client is closed.")
        try:
            if self._bus is None:
                self._bus = self._bus_factory()
            owner = self._owner()
            values = self._bus.call_blocking(
                owner,
                "/",
                _ITEM_INTERFACE,
                "GetValue",
                "",
                (),
                timeout=self._timeout,
            )
            if owner != self._owner():
                raise IntegrationError("Venus OS system service changed during the read.")
        except Exception:
            self._discard_bus()
            raise IntegrationError("Venus OS energy snapshot failed or timed out.") from None
        if not isinstance(values, Mapping):
            raise IntegrationError("Venus OS returned an invalid energy snapshot.")
        return values

    def get_energy(self) -> dict:
        started = _number(self._clock())
        values = self._snapshot()
        finished = _number(self._clock())
        if finished < started:
            raise IntegrationError("Clock changed during the Venus OS energy read.")
        elapsed = finished - started
        if not math.isfinite(elapsed):
            raise IntegrationError("Venus OS energy read has an invalid duration.")

        phases = _number(values.get("Ac/Grid/NumberOfPhases"))
        if phases not in (1, 2, 3) or set(self._grid_phases) != {
            f"L{phase}" for phase in range(1, int(phases) + 1)
        }:
            raise IntegrationError("Configured grid phases do not match the Venus OS system.")

        def metric(paths: tuple[str, ...], unit: str, *, nonnegative: bool = False) -> dict:
            readings = [_number(values.get(path)) for path in paths]
            if nonnegative and any(reading < 0 for reading in readings):
                raise IntegrationError("Venus OS returned a negative solar measurement.")
            try:
                value = math.fsum(readings)
            except OverflowError:
                raise IntegrationError(
                    "Venus OS energy sum is outside the supported range."
                ) from None
            if not math.isfinite(value):
                raise IntegrationError("Venus OS energy sum is outside the supported range.")
            return {
                "value": value,
                "unit": unit,
                "status": "fresh",
                "age_seconds": elapsed,
                "sources": [f"system/0/{path}" for path in paths],
            }

        metrics = {
            "battery_soc": metric(("Dc/Battery/Soc",), "%"),
            "battery_power": metric(("Dc/Battery/Power",), "W"),
            "solar_power": metric(self._solar_paths, "W", nonnegative=True),
            "grid_power": metric(
                tuple(f"Ac/Grid/{phase}/Power" for phase in self._grid_phases), "W"
            ),
        }
        if not 0 <= metrics["battery_soc"]["value"] <= 100 or metrics["solar_power"]["value"] < 0:
            raise IntegrationError("Venus OS energy values are outside the supported range.")
        return {
            "schema_version": 1,
            "source_type": "venus_dbus",
            "source_connected": True,
            "provenance": "local_service_read",
            "generated_at": finished,
            "metrics": metrics,
        }

    def close(self):
        self._closed = True
        self._discard_bus()
