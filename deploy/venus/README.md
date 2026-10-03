# Native Venus OS service

This is the preferred deployment when the energy system runs Venus OS. It uses
the local system D-Bus for energy and the existing Home Assistant Nest
integration for thermostat reads and setpoint commands. It needs no gateway,
container runtime, Node-RED, or Google credentials of its own.

The target must provide Python 3.12 or newer, the firmware `dbus` module, and
the standard Venus daemontools supervisor. The bundle contains locked,
architecture-independent Python HTTP dependencies; it does not change the
firmware Python environment. Raspberry Pi 3 uses those same pure Python files.
Older firmware without these prerequisites is unsupported.

## Build and install

Build on a development workstation with `uv` installed:

```sh
uv sync --frozen
bash scripts/build-venus-bundle.sh
```

Transfer `dist/inverter-climate-venus.tar.gz` and its adjacent `.sha256` file to
a private staging directory on the device. Verify and extract there:

```sh
sha256sum -c inverter-climate-venus.tar.gz.sha256
tar -xzf inverter-climate-venus.tar.gz
python3 -B inverter-climate/deploy/venus/install.py install --bundle inverter-climate
```

A first installation registers a disabled service. It writes examples to
`/data/setupOptions/inverter-climate/environment.example` and
`/data/setupOptions/inverter-climate/config.example.toml`. Copy them to
`environment` and `config.toml` in the same directory, using private permissions:

```sh
cd /data/setupOptions/inverter-climate
cp environment.example environment
cp config.example.toml config.toml
chmod 700 .
chmod 600 environment config.toml
```

Edit those files locally. The environment file uses shell `KEY=value` syntax
and contains only `HA_BASE_URL` and `HA_TOKEN`; quote values when needed. Never
publish it. Select the exact HA climate entity and keep `mode = "observe"`.
Use the HA address reachable from the device, rather than a cluster-only name.

In `[energy]`, select `backend = "venus"`, the complete set of actual solar
power paths, and every grid phase. Paths are keys from the root snapshot of
`com.victronenergy.system`, without a leading slash. Examples include
`Dc/Pv/Power` and `Ac/PvOnGrid/L1/Power`. Do not include a total together with
its component phases. Missing selected paths, incomplete phases, or an
unavailable service invalidate the observation. An absent PV source must not
be configured as though it supplies a zero value.

Start and inspect the service:

```sh
python3 -B /data/inverter-climate/deploy/venus/install.py start
svstat /service/inverter-climate
cat /run/inverter-climate/status.json
tail -n 10 /run/inverter-climate/log/current
```

Verify successive fresh status timestamps, `energy_backend = "venus"`,
`mode = "observe"`, and an empty `errors` array. Review the signs and sources
against the local system readings. Status contains private household data.
Observation mode does not change the thermostat; successful observation alone
does not enable active control.

## Files and persistence

- `/data/inverter-climate`: validated application bundle.
- `/data/inverter-climate.previous`: the preceding bundle after an update.
- `/data/setupOptions/inverter-climate`: private configuration and persistent
  service definition.
- `/data/inverter-climate-state`: ownership journal and process lock.
- `/run/inverter-climate`: status and bounded logs in RAM.
- `/service/inverter-climate`: supervisor link restored by a marked
  `/data/rc.local` hook after boot.

The installer preserves unrelated boot commands and service definitions. It
refuses unrecognized ownership or a modified bundle. Keep the installed code
immutable, and run manual Python commands with `-B` to avoid adding bytecode.
The supervised process already disables bytecode writes.

Stable observation leaves the durable journal unchanged after its initial
write. Changes to ownership, command intent, or manual override state are
persisted immediately. Owned boosts also checkpoint once per minute. Status
and logs are ephemeral and may disappear at reboot; the ownership journal is
persistent. Surplus qualification starts again after a restart.

## Stop, restart, and release

For a persistent stop, create the service's `down` file and stop the process:

```sh
touch /data/setupOptions/inverter-climate/service/down
svc -d /service/inverter-climate
svstat /service/inverter-climate
```

Wait until `svstat` reports `down` before launching any foreground instance.
Use the installer's `start` action to enable it again. Configuration is loaded
at process startup, so stop and start after a configuration change.

If active control owns a boost, stopping the process does not restore the
thermostat. After the daemon is down, retain the same active configuration and
journal and run one foreground release process:

```sh
(
  umask 077
  set -a
  . /data/setupOptions/inverter-climate/environment
  set +a
  export PYTHONPATH=/data/inverter-climate/src:/data/inverter-climate/vendor
  python3 -B -m inverter_climate.service \
    --config /data/setupOptions/inverter-climate/config.toml --release
)
```

This restores only a still-owned target, confirms the result, and exits when
idle. It cannot guarantee restoration while HA or the Nest connection is
unavailable. After successful release, switch to observation mode before
restarting. Preserve and inspect an unconfirmed-command journal; do not erase
it to force a restart. Run only one active coordinator for each thermostat,
including across different hosts.

## Update, rollback, and uninstall

Build, transfer, verify, and extract a new bundle into a separate staging
directory. Run its installer as above. An update stops the old process before
replacing its bundle, retains one previous version, and preserves both private
configuration and the enabled/disabled state. Back up configuration and the
journal privately before updates; verify journal/config compatibility before
rolling back across versions.

```sh
python3 -B /data/inverter-climate/deploy/venus/install.py rollback
svstat /service/inverter-climate
```

To unregister the service after releasing any owned target:

```sh
python3 -B /data/inverter-climate/deploy/venus/install.py uninstall
```

Uninstall stops its supervisors, removes its service link and marked boot hook,
and retains the configuration, journal, and bundles for recovery. It does not
delete household data or modify other Venus services.
