# inverter-climate

Energy-aware climate coordination **on Venus OS**, using local **D-Bus** and
**Home Assistant**. Google Nest remains connected through Home Assistant's
existing integration; this package needs no Google credentials or additional
Google project. A small supervised Python process runs beside the existing
Venus services, including on Raspberry Pi 3. It makes no D-Bus writes.

The first use case is a gas furnace that consumes approximately **500 W of
electricity while heating**. Gas supplies the heat; electricity runs the furnace
and associated equipment. This service can shift useful heating toward measured
solar export. It does **not** measure gas usage, guarantee financial savings, or
create a reason to burn additional gas just to consume electricity.

**Observation mode is the default.** It records what it would do without
changing the thermostat. Only explicit `mode = "active"` enables setpoint writes.

## Connections

```mermaid
flowchart LR
  subgraph Venus[Venus OS / Raspberry Pi]
    DBus[Local system D-Bus] --> Climate[inverter-climate]
  end
  Climate <-->|State and temperature setpoint| HA[Home Assistant]
  HA <-->|Existing Nest integration| Google[Google Nest]
  Google <--> Thermostat[Thermostat]
```

The native installation needs neither Kubernetes, Docker, inverter-gateway,
Cloudflare Access nor Node-RED. Network requests run in this separate low-rate
process, outside the inverter-control loop. HA still provides the Nest cloud
connection. This is a third-party Venus OS package, not a built-in Nest device
driver or a new thermostat page in Victron's Remote Console.

## Native Venus OS installation

Requires firmware Python 3.12+ and its native `dbus` module. Do not replace the
firmware Python or install dependencies into its global environment. The build
bundles the locked pure-Python HTTP dependencies separately; native D-Bus comes
from Venus OS. Node-RED and Venus OS Large are not required.

Build on your workstation, then transfer the archive and checksum to the device:

```sh
uv sync --frozen
bash scripts/build-venus-bundle.sh
# Transfer dist/inverter-climate-venus.tar.gz and its .sha256 to a staging directory.
# On the Venus OS device, verify the checksum before extracting:
sha256sum -c inverter-climate-venus.tar.gz.sha256
tar -xzf inverter-climate-venus.tar.gz
python3 -B inverter-climate/deploy/venus/install.py install --bundle inverter-climate
```

Installation starts disabled. Put your private configuration at
`/data/setupOptions/inverter-climate/config.toml`, using
[`examples/venus.toml`](examples/venus.toml) as the starting point. Store just
`HA_BASE_URL` and `HA_TOKEN` in the adjacent `environment` file (shell `KEY=value`
syntax, mode `0600`). Select the actual PV paths and all grid phases and retain
`mode = "observe"`. Then start the native service:

```sh
python3 -B /data/inverter-climate/deploy/venus/install.py start
svstat /service/inverter-climate
cat /run/inverter-climate/status.json
```

The installer keeps code in `/data/inverter-climate`, private configuration in
`/data/setupOptions/inverter-climate`, and the durable ownership journal in
`/data/inverter-climate-state`. A narrowly marked `/data/rc.local` hook restores
the service symlink after boot; existing boot commands remain intact. Updates
keep the previous bundle for rollback. See [native lifecycle](deploy/venus/README.md).

Status and bounded logs live in `/run` (RAM). Stable observation does not rewrite
the durable journal every poll: ownership/manual changes are saved immediately,
with a one-minute checkpoint while a boost is owned. Surplus must qualify again
after restart. This reduces background SD-card writes.

## Optional external gateway deployment

The previous external backend remains available for installations that want it.
Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
cp examples/config.toml config.toml
cp .env.example .env
# Fill .env locally; never commit it. Use your actual climate entity in config.toml.
uv run --env-file .env inverter-climate --discover
uv run --env-file .env inverter-climate --config config.toml --once
uv run --env-file .env inverter-climate --config config.toml
```

`--discover` only reads Home Assistant. `--once` exits 1 on an integration error,
2 on configuration/state failure, and 0 on a completed observation. The persistent
service continues polling on transient upstream errors, recording the reason.
Both integrations are needed before a boost is eligible; unavailable energy is
never converted to zero or inferred from other readings.

Private environment variables:

- `HA_BASE_URL`, `HA_TOKEN`: configured HA origin and long-lived access token.
- `GATEWAY_BASE_URL`, `GATEWAY_READ_TOKEN`: gateway origin and **read-only** token.
- `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`: optional Access service credentials.

Base URLs must be origins, optionally ending in `/`; proxy path prefixes are not
supported in this version. HTTPS certificate validation stays enabled, redirects
are rejected and environment proxies are ignored. HTTP is supported for a trusted
local network. Tokens are sent only to the explicitly configured origin.

## Policy

All policy temperatures use **°C**. Actual HA temperatures/commands are converted
using `/api/config`'s temperature unit. The default comfort band of 17–19 °C is an
example to review for your household, not a universal heating recommendation.

1. Respect the existing thermostat target as the baseline. Only `heat` mode,
   `idle`/`heating` action, no preset, and target-temperature capability qualify.
   `off`, cooling, heat/cool ranges, eco presets, and unavailable devices receive
   no new boost. The service never changes HVAC mode or switches furnace power.
2. Require a valid energy observation for solar, grid power, battery power and
   state of charge. Native mode reads one coherent root snapshot from
   `com.victronenergy.system`, checking the service owner around the read.
   Explicit grid phases must match `Ac/Grid/NumberOfPhases`; missing/null selected
   PV components invalidate the sample. Native freshness means a recent local
   service read, not proof of the age of a physical measurement. In gateway mode,
   the existing schema-v1 receipt freshness rules still apply.
   Positive grid power means import; negative means export.
   Positive battery power means charging. External deployments use the
   [gateway energy contract](https://github.com/victron-venus/inverter-gateway/blob/main/docs/energy-api.md).
3. Require at least 600 W of export for 180 seconds by default: 500 W estimated
   furnace draw plus 100 W margin, solar output at least 500 W, battery SoC at
   least 90%, and no battery discharge above the 50 W tolerance.
4. Raise the existing target by up to 0.5 °C, bounded by the comfort ceiling and
   device limits/step. Do nothing if the room is already warm enough. A missing
   device step uses 0.5 °C or 1 °F; for Fahrenheit devices configure a boost delta
   of at least 5/9 °C (for example 0.6) to permit one native degree.
5. Normally retain a boost for at least 15 minutes and at most 30. While already
   heating, its estimated 500 W is added back only when checking whether an
   existing boost can continue. This is an estimate, not a dedicated wattmeter.
6. Restore the original target on expiry, loss of valid energy data, excessive
   grid import/battery discharge, low SoC, or the comfort ceiling. These release
   conditions can override the minimum boost duration. Restoring a setpoint is
   not direct burner switching; the thermostat retains its own cycle protection.
7. Any observed external target/mode/preset change relinquishes ownership and
   suspends boosts for two hours. Completed restoration also starts a cooldown.

This first policy uses **measured net export only**. It does not count battery
charging as spare power, estimate curtailed PV, infer whole-house consumption,
or optimize tariffs/gas prices. A zero-export ESS may therefore never qualify
even when additional solar generation could be available. These are deliberate
limits for initial observation, to be improved with measured evidence.

## Commands and recovery

Before a service call, re-read the thermostat and persist the exact command
intent atomically. A later observed setpoint confirms the command. A timeout or
ambiguous result is **not retried**; the journal retains the intent so late
confirmation can still be recognized. A command that never confirms is shown
as `boost_unconfirmed_no_retry` or `restore_unconfirmed_no_retry` and needs local
inspection before resetting the journal. Do not delete a journal blindly.

Manual changes made through Nest, HA, a schedule or another controller are all
treated alike. HA/Nest has no compare-and-set operation; a change made between
the final read and service call can race with it. A manual write of the same
numeric target is also indistinguishable from our own confirmation. This is not
a hardware safety controller or a replacement for the thermostat's protections.

The journal binds to the HA origin and entity and survives restarts. Corruption
or an identity mismatch stops the service. A filesystem lock prevents two local
instances sharing that journal. Operate exactly **one active instance per
thermostat**; separate hosts/journals do not provide a distributed lock.

To stop active coordination cleanly, first stop the existing daemon/container
without deleting its journal or volume. A second process cannot acquire its lock.
Retain `mode = "active"` and run exactly one foreground release instance with
the same configuration, credentials and persistent state:

```sh
uv run --env-file .env inverter-climate --config config.toml --release
```

This releases only a still-owned boost and never begins another one. It exits
once the journal is idle; after confirmed restoration, switch to observation
mode before restarting your usual service.
Switching directly to observe, terminating the process, losing HA/Google access,
or powering off the host cannot guarantee immediate restoration. The thermostat
continues its own current setpoint; restarting active mode reconciles the journal.

The status JSON contains observations, the last decision, and integration health.
Logs omit endpoint/entity identity and credentials. Keep the journal on private
persistent storage and status private; on Venus, status belongs in `/run`.
Do not publish household observations. Native stop/release commands are in the
[native lifecycle guide](deploy/venus/README.md#stop-restart-and-release).

## Container and systemd

See [deployment](deploy/README.md) for Docker Compose and systemd examples.
The container runs as a non-root user; mount writable persistent `/data` and a
read-only configuration file. No public HTTP listener is opened.

## Development

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

Tests cover transport boundaries, malformed/stale data, temperature units,
control transitions, manual changes, restart journals, and uncertain command
outcomes. CI runs on Python 3.12 and 3.13. Public source contains example
identifiers only; tokens and household configuration belong outside Git.

Repository infrastructure is owned by the isolated
`terraform-github-4alvit/stacks/inverter-climate-repository` Terraform stack.

## Upstream references

- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/)
- [Home Assistant climate](https://www.home-assistant.io/integrations/climate/)
- [Google Nest integration](https://www.home-assistant.io/integrations/nest/)
- [Victron GX Opportunity Loads](https://www.victronenergy.com/media/pg/Cerbo_GX/en/gx-opportunity-loads.html)

This is an independent integration, not a native Victron Nest driver.
