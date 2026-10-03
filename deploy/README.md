# Deployment

For Venus OS, use the [native installation](venus/README.md). The alternatives
below run externally and read energy through inverter-gateway.

Start in `mode = "observe"`. Confirm HA temperature units, actual heating draw,
gateway source/sign completeness and a useful comfort band before selecting
active operation. Configure a gateway read token; the service never needs an
inverter command token.

## Docker Compose

From the repository root, create `.env` and `config.toml` using the examples.
Set these service paths in your private configuration:

```toml
state_path = "/data/state.json"
status_path = "/data/status.json"
```

Then run:

```sh
docker compose -f deploy/compose.yaml up --build -d
docker compose -f deploy/compose.yaml logs --tail=20
docker compose -f deploy/compose.yaml exec inverter-climate cat /data/status.json
```

Named volume initialization uses the container's `/data` ownership (UID 10001).
For a bind mount, provision a private directory writable by that UID instead.
Do not remove the volume to solve an unconfirmed command.

## systemd

Create a dedicated unprivileged `inverter-climate` account. Install the checkout
at `/opt/inverter-climate` and run `uv sync --frozen --no-dev` there. Store your
private configuration and environment at `/etc/inverter-climate/`, readable only
by root/the service account. Use absolute state/status paths under
`/var/lib/inverter-climate`. Install the supplied unit, reload systemd, and start
it with the usual service manager. The unit creates its persistent state directory.

There is no network control API. Status is a local JSON file. Integrations are
polled every 30 seconds by default; commands require sustained conditions and
do not poll Google directly. Run one active process per thermostat.

Stop/release instructions and uncertain-write recovery are in the root README.

## Kubernetes observation service

`kubernetes.yaml` runs one observer in its own `inverter-climate` namespace, with
UID/GID 10001, a read-only root filesystem, no API service account token, and no
Service or inbound listener. A PVC holds the journal, lock and status. The
`Recreate` rollout strategy stops the old pod before an update starts the new pod;
the journal lock also rejects another process using the same volume. Keep one
replica and stop any independently deployed instance before enabling control.

Prepare a private copy or Kustomize overlay outside the repository:

1. Replace the image's `COMMIT` placeholder with a successfully tested and
   published commit tag. Prefer its immutable `sha256` digest for deployment.
   The node selector requires Linux/amd64; use a matching published platform if
   selecting another architecture.
2. Replace `climate.replace_me` in the ConfigMap with the exact HA entity. Keep
   `mode = "observe"`, `/data/state.json` and `/data/status.json`. Use the actual
   comfort range and measured heating load.
3. Create an `inverter-climate-env` Secret in that namespace with
   `HA_BASE_URL`, `HA_TOKEN`, `GATEWAY_BASE_URL`, `GATEWAY_READ_TOKEN`,
   `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`. The Cloudflare values may
   both be empty when Access is not used. Keep the rendered Secret and actual
   entity/configuration outside Git, with local file permissions `0600` in a
   `0700` directory. Do not put tokens in shell arguments or print rendered
   manifests containing them.
4. Select and verify the storage class before creating the PVC. The example
   uses k3s `local-path` with `ReadWriteOnce`: storage follows its selected node
   and does not provide replication or failover after node loss. Back up the
   journal and inspect the class's reclaim policy before deleting a PVC or the
   namespace. Other storage must support `flock`, atomic rename and `fsync`.
   Optionally pin the workload to a known always-on node in the private overlay.
5. Verify that the chosen node can reach HA and the gateway, resolve their DNS
   names, validate their TLS certificates, and pull the image. Access policy must
   permit the machine client. The adapter sends `User-Agent: inverter-climate/0.1`.

Review and validate the public resources and private configuration before
applying them. If using a private Kustomize overlay, apply only after the exact
image has passed CI and is available to the selected node:

```sh
kubectl apply -k /absolute/path/to/private-overlay
kubectl -n inverter-climate rollout status deployment/inverter-climate --timeout=300s
kubectl -n inverter-climate logs deployment/inverter-climate --tail=20
kubectl -n inverter-climate exec deployment/inverter-climate -- cat /data/status.json
```

Logs omit credentials, endpoints and entity IDs; `status.json` contains private
household observations and should remain private. Readiness requires recent
status, successful integration reads and observation mode. Liveness checks that
the loop continues writing status, so a temporary HA/gateway outage does not
cause repeated restarts. A Ready pod therefore confirms working observation,
not approval to enable thermostat writes. Configuration is loaded at startup;
restart the Deployment after changing its ConfigMap or Secret.

Before upgrading, save a private copy of the current config and journal and
record the image digest. Roll back to that digest using the same PVC and verify
the journal schema is compatible. Do not erase state to make an upgrade start.
Activating control is a separate configuration change and requires reviewing
the observation-mode readiness guard as well as the root README's calibration,
manual override and uncertain-write instructions.
