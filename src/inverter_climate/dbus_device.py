"""Publish HA room temperature as a supported Venus OS temperature device.

Only the descriptive CustomName is writable. Thermostat state and policy are
read-only extension paths, and estimated electrical load is never presented as
measured AC power. Firmware velib_python owns D-Bus encoding and batch signals.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .clients import IntegrationError

_VELIB = Path("/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_EMPTY = {
    "/Connected": 0,
    "/Temperature": None,
    "/Climate/TargetTemperature": None,
    "/Climate/HvacMode": "unknown",
    "/Climate/HvacAction": "unknown",
    "/Climate/ServiceMode": "unknown",
    "/Climate/Phase": "unknown",
    "/Climate/Decision": "unknown",
    "/Climate/DecisionReason": "unknown",
    "/Climate/EstimatedHeatingPower": None,
    "/Climate/IntegrationHealthy": 0,
    "/Climate/LastUpdate": None,
}


def _number(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _token(value: Any) -> str:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "unknown"


def _name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and bool(value.strip())
        and value.isprintable()
    )


def _instance(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"temperature:(0|[1-9][0-9]{0,2})", value):
        result = int(value.partition(":")[2])
        return result if result <= 255 else None
    return None


def _snapshot(status: Mapping) -> dict:
    """Copy only the defined public telemetry, never entities, errors or URLs."""
    values = dict(_EMPTY)
    climate = status.get("climate")
    if isinstance(climate, Mapping):
        current = _number(climate.get("current_c"), -100, 100)
        target = _number(climate.get("target_c"), -100, 100)
        if current is not None and target is not None:
            values.update(
                {
                    "/Connected": 1,
                    "/Temperature": current,
                    "/Climate/TargetTemperature": target,
                    "/Climate/HvacMode": _token(climate.get("mode")),
                    "/Climate/HvacAction": _token(climate.get("action")),
                }
            )
    decision = status.get("decision")
    if isinstance(decision, Mapping):
        values["/Climate/Decision"] = _token(decision.get("action"))
        values["/Climate/DecisionReason"] = _token(decision.get("reason"))
    mode = status.get("mode")
    values["/Climate/ServiceMode"] = mode if mode in ("observe", "active") else "unknown"
    values["/Climate/Phase"] = _token(status.get("phase"))
    values["/Climate/EstimatedHeatingPower"] = _number(
        status.get("estimated_heating_power_w"), 0, 20000
    )
    values["/Climate/LastUpdate"] = _number(status.get("generated_at"), 0, 1e12)
    values["/Climate/IntegrationHealthy"] = int(
        values["/Connected"] == 1 and status.get("errors") == []
    )
    return values


def _firmware_number(version: str) -> int:
    match = None
    if isinstance(version, str) and len(version) <= 64:
        match = re.fullmatch(
            r"(?P<base>(?:0|[1-9][0-9]{0,2})\.(?:0|[1-9][0-9]{0,2})"
            r"\.(?:0|[1-9][0-9]{0,2}))"
            r"(?:(?:a|b|rc|\.dev)(?P<sequence>0|[1-9][0-9]{0,19}))?",
            version,
        )
    if match is None or (match["sequence"] is not None and int(match["sequence"]) > 2**64 - 2):
        raise ValueError("device firmware_version must be a canonical PEP 440 release")
    # Release plans retain their exact PEP 440 identity in GetText. Numeric
    # metadata identifies the ordered base; prereleases of a base share it.
    major, minor, patch = map(int, match["base"].split("."))
    return major * 1000000 + minor * 1000 + patch


def _temperature_text(_path, value):
    return "" if value is None else f"{value:.1f} °C"


def _runtime():
    # Use the operating system's tested D-Bus bindings and velib. Do not vendor
    # or replace them, and fail visibly if the native installation is incomplete.
    if not (_VELIB / "vedbus.py").is_file():
        raise IntegrationError("Venus OS velib_python is unavailable.")
    if str(_VELIB) not in sys.path:
        sys.path.insert(0, str(_VELIB))
    dbus = importlib.import_module("dbus")
    mainloop = importlib.import_module("dbus.mainloop.glib")
    loop = mainloop.DBusGMainLoop()
    glib = importlib.import_module("gi.repository.GLib")
    service_factory = importlib.import_module("vedbus").VeDbusService
    settings_factory = importlib.import_module("settingsdevice").SettingsDevice
    # Do not change the global default: the energy reader has its own private,
    # synchronous connection and must not be dispatched by this worker's loop.
    bus = dbus.SystemBus(private=True, mainloop=loop)
    bus.set_exit_on_disconnect(False)
    return glib, bus, service_factory, settings_factory, dbus.UInt32


class _Device:
    """D-Bus objects confined to the publisher thread."""

    def __init__(self, owner, bus, service_factory, settings_factory, uint32_factory=int):
        self.owner = owner
        self.bus = bus
        self.service_factory = service_factory
        self.uint32 = uint32_factory
        self.service = None
        self.settings = settings_factory(
            bus,
            {
                "instance": [
                    f"/Settings/Devices/{owner.device_id}/ClassAndVrmInstance",
                    f"temperature:{owner.device_instance}",
                    0,
                    0,
                ],
                "name": [
                    f"/Settings/Devices/{owner.device_id}/CustomName",
                    owner.custom_name,
                    0,
                    0,
                ],
            },
            self._setting_changed,
            timeout=5,
        )
        if _instance(self.settings["instance"]) is None or not _name(self.settings["name"]):
            raise IntegrationError("Venus OS device settings are invalid.")
        if owner._stop.is_set():
            raise IntegrationError("Venus OS device publisher is closed.")
        self.values = dict(_EMPTY)
        self._register()

    def _register(self):
        owner = self.owner
        service = self.service_factory(owner.service_name, bus=self.bus, register=False)
        self.service = service
        try:
            metadata = {
                "/Mgmt/ProcessName": "inverter-climate",
                "/Mgmt/ProcessVersion": owner.firmware_version,
                "/Mgmt/Connection": "Home Assistant room thermostat",
                "/DeviceInstance": _instance(self.settings["instance"]),
                "/ProductName": "Inverter Climate",
                "/Serial": owner.device_id,
                # Room is a supported temperature class in both current GUIs.
                "/TemperatureType": 3,
            }
            for path, value in (metadata | self.values).items():
                options = {}
                if path in ("/Temperature", "/Climate/TargetTemperature"):
                    options["gettextcallback"] = _temperature_text
                elif path == "/Climate/EstimatedHeatingPower":
                    options["gettextcallback"] = lambda _p, v: "" if v is None else f"{v:g} W"
                service.add_path(path, value, **options)
            # 0xffff is the sibling drivers' generic sentinel, not an assigned
            # Victron hardware model. Never claim an actual Victron product ID.
            service.add_path(
                "/ProductId", self.uint32(0xFFFF), gettextcallback=lambda _p, v: f"0x{v:x}"
            )
            service.add_path(
                "/FirmwareVersion",
                self.uint32(owner.firmware_number),
                gettextcallback=lambda _p, _v: owner.firmware_version,
            )
            service.add_path(
                "/HardwareVersion", self.uint32(0), gettextcallback=lambda _p, _v: "Virtual"
            )
            service.add_path(
                "/CustomName",
                self.settings["name"],
                writeable=True,
                onchangecallback=self._rename,
            )
            service.register()
        except Exception:
            self.close()
            raise

    def _rename(self, _path, value):
        if not _name(value):
            return False
        try:
            self.settings["name"] = value
        except Exception:
            return False
        return True

    def _setting_changed(self, setting, _old, value):
        if self.service is None:
            return
        try:
            if setting == "name" and _name(value):
                self.service["/CustomName"] = value
            elif setting == "instance" and _instance(value) is not None and value != _old:
                # Consumers bind DeviceInstance on NameOwnerChanged. Announce a
                # complete replacement instead of silently changing that identity.
                self.close()
                self._register()
        except Exception:
            self.owner._failed = True

    def update(self, values):
        self.values = dict(values)
        if self.service is None:
            if values["/Connected"]:
                self._register()
            return
        # The context emits one root ItemsChanged containing actual changes,
        # instead of one PropertiesChanged per path or a signal for equal values.
        with self.service as batch:
            for path, value in values.items():
                batch[path] = value

    def close(self):
        if self.service is not None:
            service, self.service = self.service, None
            service.__del__()


class DbusDevicePublisher:
    """Nonblocking bounded telemetry handoff to one GLib/D-Bus worker.

    The worker invalidates measurements when the polling thread stops producing
    snapshots. publish/check_health report a failed worker to the main process,
    allowing the supervisor to restart the complete service.
    """

    def __init__(
        self,
        identity: str,
        device_instance: int = 80,
        custom_name: str = "Inverter Climate",
        stale_seconds: float = 120,
        firmware_version: str = __version__,
        *,
        runtime_factory: Callable = _runtime,
        clock: Callable[[], float] = time.monotonic,
        startup_timeout: float = 10,
    ):
        if not isinstance(identity, str) or not identity or len(identity) > 4096:
            raise ValueError("device identity must be a nonempty bounded string")
        if type(device_instance) is not int or not 0 <= device_instance <= 255:
            raise ValueError("device_instance must be within 0..255")
        if not _name(custom_name):
            raise ValueError("device name must be a printable bounded string")
        if _number(stale_seconds, 10, 900) is None:
            raise ValueError("device stale_seconds must be within 10..900")
        if _number(startup_timeout, 0.01, 30) is None:
            raise ValueError("device startup_timeout must be within 0.01..30")
        self.device_id = "inverter_climate_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.service_name = f"com.victronenergy.temperature.{self.device_id}"
        self.device_instance = device_instance
        self.custom_name = custom_name
        self.firmware_version = firmware_version
        self.firmware_number = _firmware_number(firmware_version)
        self._stale_seconds = stale_seconds
        self._runtime_factory = runtime_factory
        self._clock = clock
        self._startup_timeout = startup_timeout
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending = None
        self._failed = False
        self._thread = None

    def start(self):
        if self._thread is not None:
            self.check_health()
            return
        if self._stop.is_set():
            raise IntegrationError("Venus OS device publisher is closed.")
        if self._runtime_factory is _runtime:
            try:
                # dbus-glib requires this before a second thread is created.
                importlib.import_module("dbus.mainloop.glib").threads_init()
            except Exception:
                raise IntegrationError("Venus OS D-Bus threading is unavailable.") from None
        self._thread = threading.Thread(target=self._run, name="inverter-climate-dbus", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            self.close()
            raise IntegrationError("Venus OS device publisher startup timed out.")
        self.check_health()

    def check_health(self):
        if (
            self._failed
            or self._stop.is_set()
            or self._thread is None
            or not self._thread.is_alive()
        ):
            raise IntegrationError("Venus OS device publisher is unavailable.")

    def publish(self, status: Mapping):
        self.check_health()
        if not isinstance(status, Mapping):
            raise IntegrationError("Venus OS device telemetry is invalid.")
        snapshot = _snapshot(status)
        with self._lock:
            # Only the latest snapshot is useful. A blocked GUI cannot cause an
            # unbounded backlog or replay old temperatures after recovery.
            self._pending = (self._clock(), snapshot)

    def _run(self):
        bus = device = glib = None
        timer = None
        try:
            glib, bus, service_factory, settings_factory, uint32 = self._runtime_factory()
            if self._stop.is_set():
                return
            loop = glib.MainLoop()
            device = _Device(self, bus, service_factory, settings_factory, uint32)
            last_update = None
            last_connected = self._clock()
            stale = True

            def disconnected(_bus):
                self._failed = True
                loop.quit()

            bus.call_on_disconnection(disconnected)

            def dispatch():
                nonlocal last_update, last_connected, stale
                try:
                    if self._stop.is_set() or self._failed:
                        loop.quit()
                        return True
                    with self._lock:
                        pending, self._pending = self._pending, None
                    if pending is not None:
                        last_update, values = pending
                        if self._clock() - last_update < self._stale_seconds:
                            device.update(values)
                            if values["/Connected"]:
                                last_connected = last_update
                            stale = False
                    if (
                        last_update is not None
                        and self._clock() - last_update >= self._stale_seconds
                        and not stale
                    ):
                        device.update(dict(_EMPTY))
                        stale = True
                    if self._clock() - last_connected >= self._stale_seconds:
                        # A vanished direct counterpart should disappear from
                        # Venus. Keep only the observer/settings subscription so
                        # a fresh observation can re-announce the same device.
                        device.close()
                    return True
                except Exception:
                    self._failed = True
                    loop.quit()
                    return True

            timer = glib.timeout_add(1000, dispatch)
            self._ready.set()
            if not self._stop.is_set():
                loop.run()
        except Exception:
            self._failed = True
        finally:
            self._ready.set()
            try:
                if timer is not None:
                    glib.source_remove(timer)
                if device is not None:
                    device.close()
            except Exception:
                self._failed = True
            finally:
                if bus is not None:
                    try:
                        bus.close()
                    except Exception:
                        self._failed = True

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
