# Venus OS SetupHelper package

This is the preferred deployment on Venus OS. The service reads local system
D-Bus energy measurements and uses the existing Home Assistant Nest integration
for thermostat reads and commands. It needs no container runtime, Node-RED, or
Google credentials of its own.

The target must provide SetupHelper, Python 3.12 or newer, the firmware `dbus`
and GLib bindings, `velib_python` (`vedbus` and `settingsdevice`) at
`/opt/victronenergy/dbus-systemcalc-py/ext/velib_python`, and Venus daemontools.
These imports are checked before an update stops the current service.
The bundle contains locked, architecture-independent
Python HTTP dependencies. It does not change the firmware Python environment;
Raspberry Pi 3 and Cerbo use the same pure Python payload. The lifecycle has been
checked against SetupHelper 9.3.

## PackageManager installation

Add `inverter-climate` from GitHub user `victron-venus`, branch `latest`, to
PackageManager. That branch contains the packaged release, including Python
dependencies. The development `main` branch is source code and is not an
installable PackageManager package.

The root `setup` script sources the installed SetupHelper `IncludeHelpers` and
finishes through `endScript`. SetupHelper records `installedVersion`, `optionsSet`
and `DO_NOT_AUTO_INSTALL` and handles its standard `install`, `uninstall`,
`reinstall`, `auto` and `runFromPm` arguments. A first installation creates a
disabled service. An update preserves the existing enabled or disabled state.
An uninstall leaves it disabled; reinstalling then requires an explicit start.

The source package remains at `/data/inverter-climate`, where PackageManager can
replace it. The running service uses a separate, checksummed payload at
`/data/inverter-climate-runtime/current`. PackageManager source replacement cannot
remove the running code or the previous runtime version. The persistent service
and marked boot hook follow the same layout as `dbus-pump`, `dbus-ev` and
`inverter-control`; SetupHelper does not install a competing firmware service.

## Build or install a verified archive manually

Build on a workstation with `uv` installed:

```sh
uv sync --frozen
bash scripts/build-venus-bundle.sh
```

The default output is `dist/inverter-climate-venus.tar.gz` and its adjacent
`.sha256` file. An optional positional argument chooses another output path.
Transfer both files to a private staging directory on the device, verify and
extract there:

```sh
sha256sum -c inverter-climate-venus.tar.gz.sha256
tar -xzf inverter-climate-venus.tar.gz
```

For a new installation with no existing package directory:

```sh
test ! -e /data/inverter-climate
mv inverter-climate /data/inverter-climate
/data/inverter-climate/setup install auto
```

Run `setup` from its canonical `/data/inverter-climate` directory. SetupHelper
locates its sibling helper resources using that directory. It must already be
installed; this package does not download or replace it. A source checkout lacks
the built `payload/` and cannot be installed directly.

### Migrate an existing 0.2 native installation

Before PackageManager or a manual transfer replaces the old `/data/inverter-climate`
directory, run the **new** installer from the verified, extracted package:

```sh
python3 -B inverter-climate/payload/deploy/venus/install.py migrate
svstat /service/inverter-climate
```

Migration copies and validates the old bundle at the isolated runtime path,
stops its process, and retargets the persistent launcher. It preserves the
bundle's original checksums, private files, observation mode and enabled state.
The old source directory stays in place. Repeating migration is safe.

Move the old source directory to a separate staging backup, then put the newly
extracted package at `/data/inverter-climate` and run `setup install auto`. That
update retains the migrated old payload as `runtime/previous`. Keep the staging
backup until observation has been verified. Never replace the legacy source
before this migration: the old process still uses that directory.

## Private configuration and first start

Installation writes examples to `/data/setupOptions/inverter-climate`. Copy them
locally with private permissions:

```sh
cd /data/setupOptions/inverter-climate
cp environment.example environment
cp config.example.toml config.toml
chmod 700 .
chmod 600 environment config.toml
```

Edit these files on the device. `environment` uses shell `KEY=value` syntax and
contains only `HA_BASE_URL` and `HA_TOKEN`; quote values when needed. Never publish
it. Select the exact HA climate entity and keep `mode = "observe"`. Use the HA
address reachable from the device.

In `[energy]`, select `backend = "venus"`, the complete set of actual solar power
paths, and every grid phase. Paths are root snapshot keys from
`com.victronenergy.system`, without a leading slash, such as `Dc/Pv/Power` and
`Ac/PvOnGrid/L1/Power`. Do not include a total together with its component phases.
Missing selected paths, incomplete phases, or an unavailable service invalidate
the observation. Do not configure an absent PV source as a zero-valued source.

```sh
python3 -B /data/inverter-climate/payload/deploy/venus/install.py start
svstat /service/inverter-climate
cat /run/inverter-climate/status.json
tail -n 10 /run/inverter-climate/log/current
```

Verify successive fresh timestamps, `energy_backend = "venus"`, `mode = "observe"`
and an empty `errors` array. Review measurement signs and sources against local
system readings. Status contains private household data. Successful observation
does not enable thermostat control.

## Optional native manual controls

GUI v2's Switch pane supports a temperature slider and a Heating mode dropdown
(`Off` / `Heat`) for this package. The same controls are accessible through VRM
Remote Console. They use the existing Home Assistant integration and need no
Node-RED service or GUI modification.

In the private `config.toml`, add `control_enabled = true` to the existing
`[device]` section. Keep `[service] mode = "observe"` to leave automatic
preheating disabled. Configuration is loaded on startup, so restart the running
service with `svc -t /service/inverter-climate`. Verify fresh healthy status and
the controls in Switch pane before changing a setting. Installation and updates
preserve this explicit choice; new installations keep controls disabled.

D-Bus target and mode readback contain confirmed HA observations. A queued
request does not immediately change them. The stock GUI can briefly show a
requested value while waiting; that preview is not a thermostat confirmation. Commands are validated again against
fresh HA state and persisted before sending. An uncertain outcome is never
automatically retried. When Nest is off and supplies no target temperature, the
slider is hidden and the Heating mode dropdown remains available.

Before rolling back to version 0.3, disable manual controls and remove the
`control_enabled` configuration key, which that version does not recognize.
Keep the manual-command journal for diagnosis, and do not roll back while a
command outcome is uncertain. Automatic policy mode should remain `observe`.

## Files and persistence

- `/data/inverter-climate`: PackageManager source package and lifecycle entrypoints.
- `/data/inverter-climate-runtime/current`: validated running payload.
- `/data/inverter-climate-runtime/previous`: preceding payload for rollback.
- `/data/setupOptions/inverter-climate`: private configuration, SetupHelper options
  and persistent service definition.
- `/data/inverter-climate-state`: durable ownership journal, separate
  `state.json.manual` command journal and process lock.
- `/run/inverter-climate`: status and bounded logs in RAM.
- `/service/inverter-climate`: supervisor link restored by its marked
  `/data/rc.local` hook after boot.

The installer preserves unrelated boot commands and services. It rejects
unrecognized ownership or a modified payload. Keep runtime files immutable;
manual Python commands should use `-B` to avoid bytecode writes. The supervised
process already disables them.

Stable observation leaves the durable journal unchanged after its initial write.
Ownership, command intent and manual override changes persist immediately; owned
boosts checkpoint once per minute. Status and logs disappear at reboot. Surplus
qualification starts again after restart.

## Stop, restart, and release

For a persistent stop:

```sh
touch /data/setupOptions/inverter-climate/service/down
svc -d /service/inverter-climate
svstat /service/inverter-climate
```

Wait for `down` before launching a foreground instance. Use the native installer's
`start` action to enable it again. Configuration is loaded at process startup.

If active control owns a boost, stopping does not restore the thermostat. Once
the daemon is down, retain the active configuration and journal and run one
foreground release process:

```sh
(
  umask 077
  set -a
  . /data/setupOptions/inverter-climate/environment
  set +a
  export PYTHONPATH=/data/inverter-climate-runtime/current/src:/data/inverter-climate-runtime/current/vendor
  python3 -B -m inverter_climate.service \
    --config /data/setupOptions/inverter-climate/config.toml --release
)
```

This restores only a still-owned target, confirms the result, and exits when idle.
Restoration needs a working HA/Nest connection. After successful release, switch
to observation before restarting. Preserve an unconfirmed-command journal; do
not erase it to force a restart. Run one active coordinator per thermostat,
including across different hosts.

## Update, emergency rollback, and uninstall

Use PackageManager to download and install subsequent releases. Manual updates
use the same verified archive and `setup install auto` entrypoint after replacing
only the source package. The installer checks the new payload and firmware
prerequisites before stopping the existing process, then retains one previous
version. Private configuration, journal and enabled state survive updates.

Back up private data before an update and verify journal/config compatibility
before rolling back across versions. Emergency runtime rollback uses the
installer in the source package:

```sh
python3 -B /data/inverter-climate/payload/deploy/venus/install.py rollback
svstat /service/inverter-climate
```

This changes the running payload; the PackageManager source and its installed
package version marker still describe the downloaded package. Disable automatic
installation while investigating, then reconcile the desired package version
through SetupHelper. A later package installation can replace the rolled-back
runtime again.

If an interrupted update leaves `runtime/current` missing, the same command works
from the source package or from a verified extracted package. The installer
restores the previous payload after failed promotion when possible and resumes
recognized intermediate states of an interrupted swap. Ambiguous states retain
all copies for inspection. Do not delete recovery directories or install over an
incomplete update.

After releasing any owned target, uninstall through PackageManager or:

```sh
/data/inverter-climate/setup uninstall auto
```

Uninstall stops its supervisors and removes its own link and boot hook. SetupHelper
clears the installed version and records `DO_NOT_AUTO_INSTALL`. Private
configuration, journal and runtime copies remain for recovery. Firmware-triggered
`setup reinstall auto` follows SetupHelper's standard version logic; it preserves
configuration and the service's enabled state.

For offline tests, the native installer accepts `--root <temporary-directory>`.
Package entrypoint tests use `INVERTER_CLIMATE_ROOT` with a temporary helper fixture.
No supervisor commands run with an offline root.
