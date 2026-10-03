"""Publish HA room temperature as a supported Venus OS temperature device.

Optional switch controls enqueue validated thermostat requests without changing
observed telemetry. Estimated electrical load is never presented as measured AC
power. Firmware velib_python owns D-Bus encoding and batch signals.
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
_TEMPERATURE = "/SwitchableOutput/0"
_MODE = "/SwitchableOutput/1"
_HVAC_MODE = "/Climate/HvacMode"
_CONTROL_ENABLED = "/Climate/ControlEnabled"
_CONTROL_AVAILABLE = "/Climate/ControlAvailable"
_CONTROL_PENDING = "/Climate/ControlPending"
_CONTROL_OUTCOME = "/Climate/ControlOutcome"
_CONTROL_REASON = "/Climate/ControlReason"
_DISABLED = 0x20
_EMPTY = {
    "/Connected": 0,
    "/Temperature": None,
    "/Climate/TargetTemperature": None,
    _HVAC_MODE: "unknown",
    "/Climate/HvacAction": "unknown",
    "/Climate/ServiceMode": "unknown",
    "/Climate/Phase": "unknown",
    "/Climate/Decision": "unknown",
    "/Climate/DecisionReason": "unknown",
    "/Climate/EstimatedHeatingPower": None,
    "/Climate/IntegrationHealthy": 0,
    "/Climate/LastUpdate": None,
    _CONTROL_ENABLED: 0,
    _CONTROL_AVAILABLE: 0,
    _CONTROL_PENDING: 0,
    _CONTROL_OUTCOME: "idle",
    _CONTROL_REASON: "disabled",
    f"{_TEMPERATURE}/Dimming": None,
    f"{_TEMPERATURE}/Measurement": None,
    f"{_TEMPERATURE}/Status": _DISABLED,
    f"{_TEMPERATURE}/Settings/DimmingMin": None,
    f"{_TEMPERATURE}/Settings/DimmingMax": None,
    f"{_TEMPERATURE}/Settings/StepSize": 0.5,
    f"{_MODE}/Dimming": None,
    f"{_MODE}/Status": _DISABLED,
}
_CONTROL_KEYS = tuple(
    path
    for path in _EMPTY
    if path.startswith("/Climate/Control") or path.endswith("/Status") or "/Settings/" in path
)


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


def _snapshot(status: Mapping, *, commands_supported: bool = False) -> dict:
    """Copy only the defined public telemetry, never entities, errors or URLs."""
    values = dict(_EMPTY)
    climate = status.get("climate")
    if isinstance(climate, Mapping):
        current = _number(climate.get("current_c"), -100, 100)
        target = _number(climate.get("target_c"), -100, 100)
        if current is not None:
            values.update(
                {
                    "/Connected": 1,
                    "/Temperature": current,
                    "/Climate/TargetTemperature": target,
                    _HVAC_MODE: _token(climate.get("mode")),
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
    _control_snapshot(status.get("control"), values, commands_supported)
    return values


def _control_snapshot(control, values, commands_supported):
    """Advertise observed values and only capabilities backed by the broker."""
    mode = values[_HVAC_MODE]
    if values["/Connected"]:
        values[f"{_TEMPERATURE}/Measurement"] = values["/Temperature"]
        if mode == "heat":
            values[f"{_TEMPERATURE}/Dimming"] = values["/Climate/TargetTemperature"]
        values[f"{_MODE}/Dimming"] = {"off": 0, "heat": 1}.get(mode)
    if not commands_supported or not isinstance(control, Mapping):
        return
    enabled = control.get("enabled") is True
    available = enabled and control.get("available") is True and bool(values["/Connected"])
    values[_CONTROL_ENABLED] = int(enabled)
    values[_CONTROL_AVAILABLE] = int(available)
    values[_CONTROL_PENDING] = int(control.get("pending") is True)
    outcome = control.get("outcome")
    if outcome in ("idle", "queued", "pending", "confirmed", "rejected", "unconfirmed"):
        values[_CONTROL_OUTCOME] = outcome
    values[_CONTROL_REASON] = _token(control.get("reason"))
    minimum = _number(control.get("min_c"), -100, 100)
    maximum = _number(control.get("max_c"), -100, 100)
    step = _number(control.get("step_c"), 0, 5)
    if minimum is not None and maximum is not None and minimum < maximum and step:
        values[f"{_TEMPERATURE}/Settings/DimmingMin"] = minimum
        values[f"{_TEMPERATURE}/Settings/DimmingMax"] = maximum
        values[f"{_TEMPERATURE}/Settings/StepSize"] = step
        if (
            available
            and control.get("can_set_temperature") is True
            and values[f"{_TEMPERATURE}/Dimming"] is not None
        ):
            values[f"{_TEMPERATURE}/Status"] = 0x09
    supported = control.get("supported_modes")
    if (
        available
        and control.get("can_set_mode") is True
        and isinstance(supported, (tuple, list))
        and "heat" in supported
        and "off" in supported
        and values[f"{_MODE}/Dimming"] is not None
    ):
        values[f"{_MODE}/Status"] = 0x09


def _command_item_type(base, method, unwrap):
    """Keep velib read/notification behavior, but make SetValue a request only.

    Standard velib SetValue both skips callbacks for equal values and echoes an
    accepted request into telemetry. Neither behavior is valid for HA commands.
    """

    class CommandItem(base):
        @method("com.victronenergy.BusItem", in_signature="v", out_signature="i")
        def SetValue(self, newvalue):
            if not self._writeable or self._onchangecallback is None:
                return 1
            try:
                accepted = self._onchangecallback(self._path, unwrap(newvalue))
            except Exception:
                return 2
            return 0 if accepted is True else 2

    return CommandItem


class _TemperatureUnit:
    """One read-only subscription to the shared GUI temperature preference."""

    def __init__(self, bus, item_factory, callback):
        self.bus, self.item_factory, self.callback = bus, item_factory, callback
        self.item = None
        self.value = None
        self.match = bus.add_signal_receiver(
            self._owner_changed,
            signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus",
            bus_name="org.freedesktop.DBus",
            arg0="com.victronenergy.settings",
        )
        self._connect()

    def _connect(self):
        if self.item is not None:
            self.item.__del__()
            self.item = None
        try:
            self.item = self.item_factory(
                self.bus,
                "com.victronenergy.settings",
                "/Settings/System/Units/Temperature",
                eventCallback=self._changed,
            )
            self.value = self.item.get_value()
        except Exception:
            self.value = None

    def _changed(self, _service, _path, changes):
        self.value = changes.get("Value")
        self.callback(self.value)

    def _owner_changed(self, _name, _old, new):
        if new:
            self._connect()
        else:
            self.value = None
        self.callback(self.value)

    def close(self):
        self.match.remove()
        if self.item is not None:
            self.item.__del__()
            self.item = None


def _firmware_version(version: str) -> str:
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
    return version


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
    vedbus = importlib.import_module("vedbus")
    service_factory = vedbus.VeDbusService
    command_item = _command_item_type(
        vedbus.VeDbusItemExport,
        importlib.import_module("dbus.service").method,
        vedbus.unwrap_dbus_value,
    )
    settings_factory = importlib.import_module("settingsdevice").SettingsDevice
    # Do not change the global default: the energy reader has its own private,
    # synchronous connection and must not be dispatched by this worker's loop.
    bus = dbus.SystemBus(private=True, mainloop=loop)
    bus.set_exit_on_disconnect(False)

    def temperature_unit_factory(bus, callback):
        return _TemperatureUnit(bus, vedbus.VeDbusItemImport, callback)

    return (
        glib,
        bus,
        service_factory,
        settings_factory,
        dbus.UInt32,
        command_item,
        temperature_unit_factory,
    )


class _Device:
    """D-Bus objects confined to the publisher thread."""

    def __init__(
        self,
        owner,
        bus,
        service_factory,
        settings_factory,
        uint32_factory=int,
        command_item=None,
        temperature_unit_factory=None,
    ):
        self.owner = owner
        self.bus = bus
        self.service_factory = service_factory
        self.uint32 = uint32_factory
        self.command_item = command_item
        self.received_at = None
        self.temperature_unit = None
        self.unit_setting = None
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
        if temperature_unit_factory is not None and owner.command_broker is not None:
            self.unit_setting = temperature_unit_factory(bus, self._unit_changed)
            self.temperature_unit = self.unit_setting.value
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
            for prefix, kind, label in (
                (_TEMPERATURE, 3, "Temperature setpoint"),
                (_MODE, 6, "Heating mode"),
            ):
                metadata.update(
                    {
                        f"{prefix}/Name": label,
                        f"{prefix}/State": None,
                        f"{prefix}/Settings/Type": kind,
                        f"{prefix}/Settings/ValidTypes": 1 << kind,
                        f"{prefix}/Settings/Adjustable": 0,
                        # GUI 1.2.40 predates Adjustable. Invalid optional fields
                        # hide its setting editors and use device/name defaults.
                        f"{prefix}/Settings/Group": None,
                        f"{prefix}/Settings/CustomName": None,
                        f"{prefix}/Settings/ShowUIControl": None,
                    }
                )
            metadata.update(
                {
                    f"{_TEMPERATURE}/Settings/Decimals": 1,
                    f"{_MODE}/Settings/Labels": ["Off", "Heat"],
                    f"{_MODE}/Settings/DimmingMin": 0,
                    f"{_MODE}/Settings/DimmingMax": 1,
                    f"{_MODE}/Settings/StepSize": 1,
                    f"{_MODE}/Settings/Decimals": 0,
                }
            )
            for path, value in (metadata | self.values).items():
                service.add_path(path, value, **self._path_options(path))
            # 0xffff is the sibling drivers' generic sentinel, not an assigned
            # Victron hardware model. Never claim an actual Victron product ID.
            service.add_path(
                "/ProductId", self.uint32(0xFFFF), gettextcallback=lambda _p, v: f"0x{v:x}"
            )
            service.add_path(
                "/FirmwareVersion",
                # GUI v2 reads the raw value over MQTT and treats integers as
                # Victron hex/BCD versions. It explicitly preserves strings;
                # use that supported path for our full PEP 440 identity.
                owner.firmware_version,
                gettextcallback=lambda _p, _v: owner.firmware_version,
            )
            service.add_path(
                "/HardwareVersion", "Virtual", gettextcallback=lambda _p, _v: "Virtual"
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

    def _path_options(self, path):
        options = {}
        if path in ("/Temperature", "/Climate/TargetTemperature"):
            options["gettextcallback"] = _temperature_text
        elif path == "/Climate/EstimatedHeatingPower":
            options["gettextcallback"] = lambda _p, v: "" if v is None else f"{v:g} W"
        if (
            path in (f"{_TEMPERATURE}/Dimming", f"{_MODE}/Dimming")
            and self.owner.command_broker is not None
        ):
            if self.command_item is None:
                raise IntegrationError("Venus OS command item is unavailable.")
            options.update(
                writeable=True,
                onchangecallback=self._command,
                itemtype=self.command_item,
            )
        return options

    def _rename(self, _path, value):
        if not _name(value):
            return False
        try:
            self.settings["name"] = value
        except Exception:
            return False
        return True

    def _command(self, path, value):
        broker = self.owner.command_broker
        if (
            broker is None
            or self.service is None
            or self.owner._stop.is_set()
            or self.owner._failed
            or self.received_at is None
            or not 0 <= self.owner._clock() - self.received_at < self.owner._stale_seconds
            or not self.values[_CONTROL_AVAILABLE]
        ):
            return False
        try:
            if self._submit_request(broker, path, value) is not True:
                return False
            # Queued requests remain coalescible. Only a subsequent broker
            # snapshot can disable controls or confirm the observed result.
            queued = self.values | {
                _CONTROL_PENDING: 1,
                _CONTROL_OUTCOME: "queued",
                _CONTROL_REASON: "queued",
            }
        except Exception:
            return False
        try:
            self._write_values(queued, self.received_at)
        except Exception:
            # The broker already accepted the request. A failed GUI update must
            # not report rejection of a command that can still execute.
            self.owner._failed = True
        return True

    def _submit_request(self, broker, path, value):
        if path == f"{_TEMPERATURE}/Dimming":
            if self.values[f"{_TEMPERATURE}/Status"] & _DISABLED:
                return False
            target = _number(
                value,
                self.values[f"{_TEMPERATURE}/Settings/DimmingMin"],
                self.values[f"{_TEMPERATURE}/Settings/DimmingMax"],
            )
            if target is None:
                return False
            # The broker validates HA's native unit/step lattice; never round
            # a GUI request into a different temperature here.
            return broker.submit_temperature(target)
        if path == f"{_MODE}/Dimming":
            if self.values[f"{_MODE}/Status"] & _DISABLED:
                return False
            mode = _number(value, 0, 1)
            if mode not in (0, 1):
                return False
            return broker.submit_mode("heat" if mode == 1 else "off")
        return False

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

    def update(self, values, received_at=None):
        values = dict(values)
        self._apply_temperature_unit(values)
        self._write_values(values, received_at)

    def _apply_temperature_unit(self, values):
        if self.temperature_unit == "fahrenheit":
            # GUI 1.2.40 and current gui-v2 main convert temperatures and bounds,
            # but deliberately interpret StepSize in the GUI's display unit.
            values[f"{_TEMPERATURE}/Settings/StepSize"] *= 1.8
        elif self.temperature_unit != "celsius":
            values[f"{_TEMPERATURE}/Status"] = _DISABLED

    def _unit_changed(self, value):
        self.temperature_unit = value
        try:
            self.refresh_controls()
        except Exception:
            self.owner._failed = True

    def refresh_controls(self):
        broker = self.owner.command_broker
        if broker is None or self.service is None:
            return
        control = broker.status()
        if not isinstance(control, Mapping):
            return
        values = dict(self.values)
        values.update({path: _EMPTY[path] for path in _CONTROL_KEYS})
        _control_snapshot(control, values, True)
        self._apply_temperature_unit(values)
        if (
            self.received_at is None
            or not 0 <= self.owner._clock() - self.received_at < self.owner._stale_seconds
        ):
            values[_CONTROL_AVAILABLE] = 0
            values[_CONTROL_REASON] = "telemetry_stale"
            values[f"{_TEMPERATURE}/Status"] = _DISABLED
            values[f"{_MODE}/Status"] = _DISABLED
        # Control flags may change while HA I/O blocks the main thread. This
        # refresh must never extend observation freshness or change readback.
        self._write_values(
            self.values | {path: values[path] for path in _CONTROL_KEYS}, self.received_at
        )

    def _write_values(self, values, received_at=None):
        self.values = dict(values)
        self.received_at = received_at
        if self.service is None:
            if values["/Connected"]:
                self._register()
            return
        # The context emits one root ItemsChanged containing actual changes,
        # instead of one PropertiesChanged per path or a signal for equal values.
        with self.service as batch:
            for path, value in values.items():
                batch[path] = value

    def invalidate(self):
        # Loss of telemetry cannot prove that an outstanding request finished.
        # Preserve its last reported state while disabling every command path.
        self.update(
            _EMPTY
            | {
                path: self.values[path]
                for path in (
                    _CONTROL_ENABLED,
                    _CONTROL_PENDING,
                    _CONTROL_OUTCOME,
                )
            }
            | {_CONTROL_REASON: "telemetry_stale"}
        )

    def close(self, *, final=False):
        if self.service is not None:
            service, self.service = self.service, None
            service.__del__()
        if final and self.unit_setting is not None:
            self.unit_setting.close()
            self.unit_setting = None


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
        command_broker=None,
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
        if command_broker is not None and not all(
            callable(getattr(command_broker, name, None))
            for name in ("submit_temperature", "submit_mode", "status")
        ):
            raise ValueError("device command_broker must support thermostat requests")
        self.command_broker = command_broker
        self.device_id = "inverter_climate_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.service_name = f"com.victronenergy.temperature.{self.device_id}"
        self.device_instance = device_instance
        self.custom_name = custom_name
        self.firmware_version = _firmware_version(firmware_version)
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
        snapshot = _snapshot(status, commands_supported=self.command_broker is not None)
        with self._lock:
            # Only the latest snapshot is useful. A blocked GUI cannot cause an
            # unbounded backlog or replay old temperatures after recovery.
            self._pending = (self._clock(), snapshot)

    def _run(self):
        bus = device = glib = None
        timer = None
        try:
            glib, bus, service_factory, settings_factory, uint32, command_item, unit_factory = (
                self._runtime_factory()
            )
            if self._stop.is_set():
                return
            loop = glib.MainLoop()
            device = _Device(
                self, bus, service_factory, settings_factory, uint32, command_item, unit_factory
            )
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
                            device.update(values, last_update)
                            if values["/Connected"]:
                                last_connected = last_update
                            stale = False
                    device.refresh_controls()
                    if (
                        last_update is not None
                        and self._clock() - last_update >= self._stale_seconds
                        and not stale
                    ):
                        device.invalidate()
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
                    device.close(final=True)
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
