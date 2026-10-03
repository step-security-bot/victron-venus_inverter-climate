# Venus OS D-Bus device

Native continuous operation publishes a supported
`com.victronenergy.temperature.inverter_climate_<identity-hash>` service. Its
product name is **Inverter Climate** and its temperature type is **Room** (3).
Both Venus GUIs support temperature devices. The stock device page shows room
temperature and device information. GUI v2 additionally renders optional native
Switch pane controls through the standard Switchable Output API on this service.
The additional `/Climate/*` telemetry does not itself create controls.

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
complete service so consumers discover the new instance. `/CustomName` updates
local settings. Thermostat commands are accepted only through the explicitly
enabled Switchable Output paths described below; telemetry remains read-only.

`ProductId` uses `0xffff`, matching the sibling projects' generic sentinel. This
is not an assigned Victron product ID or a claim to be Victron hardware.
`GetText` returns hexadecimal product identity. Firmware `GetValue` and `GetText`
both return the full canonical PEP 440 release string, including the release
toolkit's `0.3.0b1` and `0.3.0.dev1000001` projections. Hardware version is the
string `Virtual` in both representations.

Although the general D-Bus API recommends numeric firmware versions, GUI v2
[formats raw integer versions as Victron hex/BCD and explicitly preserves string versions](https://github.com/victronenergy/gui-v2/blob/main/components/FirmwareVersion.qml).
Its [device information item](https://github.com/victronenergy/gui-v2/blob/main/components/listitems/ListFirmwareVersion.qml)
uses the raw value, not `GetText`. A decimal encoding such as 3000 therefore
appears as `vB.B8`. Publishing the supported string representation keeps the
actual three-part application version and prerelease identity visible in both
GUI generations. This virtual device does not advertise Victron firmware-update
interfaces that need a numeric hardware firmware version.

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

Publishing a device does not enable control. The automatic policy requires
`mode = "active"`, while direct user commands independently require
`[device] control_enabled = true`. One-shot, discovery and release commands do
not create a temporary GUI device.

## Native manual controls

GUI v2 1.2.40 supports both controls in its standard Switch pane. The
[Victron Switchable Output API](https://github.com/victronenergy/venus/wiki/dbus#switch)
allows these paths on an existing temperature service, so the original device
instance and temperature history remain unchanged.

- `/SwitchableOutput/0` is a temperature setpoint control (`Settings/Type = 3`).
  `Dimming` holds the observed target in Celsius, and `Measurement` holds the
  observed room temperature. Device limits and step determine slider bounds.
- `/SwitchableOutput/1` is a dropdown (`Settings/Type = 6`) labelled Heating mode.
  Its labels are `Off` and `Heat`, with corresponding `Dimming` values 0 and 1.

Control settings are fixed (`Settings/Adjustable = 0`). The writable `Dimming`
items validate and enqueue requests; they do not call HA from the D-Bus thread.
They also do not replace observed values with requested values when a write is
accepted. The publisher continues batched, change-only updates from observations.
Stock GUI widgets can briefly preview a requested value while waiting; that is
not a confirmed thermostat observation.

GUI v2 1.2.40 converts temperature values and bounds to its display unit but uses
`Settings/StepSize` directly. One read-only subscription to the shared
`/Settings/System/Units/Temperature` setting adjusts that UI step for Celsius or
Fahrenheit. The firmware's empty unit preference uses Celsius, matching the
stock GUI default. Targets, measurements and bounds remain Celsius on D-Bus. An unknown
display unit disables the slider while leaving supported mode control available.

Manual control requires fresh HA capabilities, a supported value and no pending
or uncertain command. The coordinator validates again before persisting intent
and sending one HA request. Manual input takes priority over automatic preheat
and starts the configured manual hold. Requests are not replayed after restart
or blindly retried after a timeout. Successful submission alone is not proof
that the thermostat has applied a command.

When Nest is off, its target temperature may be absent. `/Temperature` and
`/Connected` remain valid, the setpoint slider is hidden, and the mode dropdown
can turn heating back on. Unsupported modes and stale or unavailable HA data
disable control. The classic GUI remains a temperature display; remote control
uses GUI v2, including VRM Remote Console.

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
