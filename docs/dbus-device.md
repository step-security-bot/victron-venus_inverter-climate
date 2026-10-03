# Venus OS D-Bus device

Native continuous operation publishes a supported
`com.victronenergy.temperature.inverter_climate_<identity-hash>` service. Its
product name is **Inverter Climate** and its temperature type is **Room** (3).
Both Venus GUIs support temperature devices. The stock device page shows room
temperature and device information; it does not provide a thermostat control
screen for the additional climate paths.

The identity hash is derived from the configured thermostat binding. The service
does not publish the HA URL, entity ID, token, or upstream response bodies.
`DeviceInstance` defaults to 80 and must be unique among temperature devices.
Check existing instances before installation. Changing the HA origin or entity
changes the device identity as well as the journal binding.

## Metadata and settings

The service supplies `/Mgmt/ProcessName`, `/Mgmt/ProcessVersion`,
`/Mgmt/Connection`, `/DeviceInstance`, `/ProductId`, `/ProductName`,
`/FirmwareVersion`, `/HardwareVersion`, `/Serial`, `/CustomName`, `/Connected`
and `/TemperatureType` before announcing its bus name.

Firmware `SettingsDevice` persists the name and
`ClassAndVrmInstance = "temperature:<instance>"` under
`/Settings/Devices/inverter_climate_<identity-hash>`. Existing settings win over
configuration defaults. A valid instance change withdraws and re-announces the
complete service so consumers discover the new instance. Only `/CustomName` is
writable on the device, and changing it updates local settings. No device path
can issue a thermostat command.

`ProductId` uses `0xffff`, matching the sibling projects' generic sentinel. This
is not an assigned Victron product ID or a claim to be Victron hardware.
`GetText` returns hexadecimal product identity and the human-readable application
version. The numeric firmware version is
`major * 1_000_000 + minor * 1_000 + patch`, with each release component limited
to 0–999. Canonical PEP 440 alpha, beta, release-candidate and development
suffixes are accepted, including the release toolkit's `0.3.0b1` and
`0.3.0.dev1000001` projections. Prereleases share their numeric base; `GetText`
retains the full release identity. Hardware version is zero with the text `Virtual`.

The [Victron D-Bus API](https://github.com/victronenergy/venus/wiki/dbus-api)
prefers unsigned 32-bit product identity. The application supplies a UInt32,
but the firmware's `velib_python` converts integers to signed Int32/Int64 when
encoding individual values and root snapshots. The numeric value remains
65535. This package retains the firmware's encoding and exporters rather than
replacing them solely for signedness. Invalid values likewise use the firmware's
standard empty D-Bus array and `---` display text.

## Read-only observations

`/Temperature` is the observed room temperature in Celsius. The following
extension paths describe the existing coordinator and HA observations:

- `/Climate/TargetTemperature`: observed target in Celsius.
- `/Climate/HvacMode` and `/Climate/HvacAction`: HA mode and current action.
- `/Climate/ServiceMode`: `observe` or `active`.
- `/Climate/Phase`: coordinator ownership phase.
- `/Climate/Decision` and `/Climate/DecisionReason`: latest policy result.
- `/Climate/EstimatedHeatingPower`: configured electrical load estimate in watts.
- `/Climate/IntegrationHealthy`: successful climate observation with no integration errors.
- `/Climate/LastUpdate`: latest completed coordinator cycle, as a Unix timestamp.

Temperature `GetText` values include °C and the load estimate includes W.
The estimate is not a power measurement. The service publishes no `/Ac`, `/Dc`
or energy counters, and therefore does not add a fictitious measured load to
Victron totals. A gas furnace can have substantial electrical auxiliary demand;
the configured estimate describes that demand, not its gas heating output.

Publishing a device does not enable control. Only the existing coordinator's
explicit active policy can send HA thermostat commands. One-shot, discovery and
release commands do not create a temporary GUI device.

## Freshness and recovery

A failed climate read immediately invalidates the temperature and target and
sets `/Connected` to zero. A failed energy read leaves a valid room temperature
available, while `/Climate/IntegrationHealthy` becomes zero. Missing or malformed
temperature values never become a plausible zero reading.

If no valid climate observation arrives within `[device].stale_seconds`, the
temperature service withdraws from the bus. This also covers a stalled polling
thread. The observer continues trying HA; a valid new observation re-announces
the complete device under its stable identity. Settings remain available across
this interruption. A D-Bus worker failure is reported to the main process so the
supervisor can restart it.

## Connections and update cost

There are two persistent private bus connections. The synchronous energy reader
reads one system-service root snapshot per polling cycle, with owner checks
before and after it. The device publisher owns a separate GLib worker and private
connection. D-Bus threading is initialized before that worker starts; its explicit
main loop does not change the energy reader's global loop configuration.

The handoff holds only the latest normalized snapshot. The publisher uses the
firmware `VeDbusService` batch context to emit one root `ItemsChanged` signal for
changed paths per update. Its one-second expiry timer does not emit telemetry
when nothing changed. Normal polling does not re-register services or rewrite
settings. There is no subscription to all energy signals, growing history cache,
or repeated `dbus-send` subprocess.

The implementation follows the firmware
[velib_python service and batch API](https://github.com/victronenergy/velib_python/blob/master/vedbus.py),
the [temperature service contract](https://github.com/victronenergy/venus/wiki/dbus),
and [dbus-python's GLib threading requirements](https://dbus.freedesktop.org/doc/dbus-python/dbus.mainloop.html).
