# Native release packaging

The shared [Venus OS CI Toolkit](https://github.com/victron-venus/venus-os-ci-toolkit)
generates `quality-gate.yml`, `release-pipeline.yml`, version adapters, provenance
checks and release contract tests from `.release-policy.json`. The reviewed toolkit
revision is recorded in that policy. Keep generated files synchronized by running
that revision's `scripts/install_release.py` against this repository; use `--check`
to detect drift. Project-specific adapters are `release-build.yml`,
`scripts/package_release.py`, and `scripts/publish_setuphelper.py`.

`bash scripts/ci.sh` runs frozen project checks, release contracts, version checks
and the native build. GitHub runs Python 3.12 and 3.13, CodeQL, Trivy and the shared
required **CI gate** before release qualification. Native hardware acceptance is
separate; hosted runners do not supply the Venus OS D-Bus ABI.

## Assets and versions

`bash scripts/package-release.sh 0.3.0 rc` builds locally from declared Git-tracked
files. The output directory must be empty. A hosted build first applies the saved
version plan; beta/nightly overlays change every declared version field before
packaging. RC packages already contain the stable base so promotion can copy exact
bytes. The root `version` has a leading `v`, which SetupHelper requires for package
discovery; Python metadata uses the corresponding PEP 440 value.

Each release contains:

- `inverter-climate-<package-version>.tar.gz`: SetupHelper wrapper and checksummed
  `payload/`, including frozen architecture-independent HTTP dependencies.
- The archive's adjacent `.sha256` file and a complete `SHA256SUMS` inventory.
- `inverter_climate-<python-version>-py3-none-any.whl`: Python distribution for
  companion-host use; dependencies remain external for that installation method.
- Shared release input receipts and `release-manifest.json` provenance evidence.

Only the firmware supplies `dbus`. The native builder rejects non-pure wheels,
native binaries and bytecode. Release packaging uses an isolated Git-tracked
snapshot and excludes untracked credentials, private configuration and runtime
journals. The wheel is a release asset; no workflow automatically uploads to PyPI.

## SetupHelper distribution branch

SetupHelper 9.3 downloads GitHub **branch archives**, not release assets. Therefore
`gitHubInfo` selects `victron-venus:latest`, and `latest` contains the complete
verified package tree, including `payload/vendor/`. The `main` branch and immutable
release tags remain developer source history.

After a successful Release pipeline, `setuphelper-publish.yml` checks the current
latest stable release with the shared toolkit's `verified_assets` implementation.
It verifies the original RC run, source policy, immutable evidence, receipt
inventory and downloaded hashes, validates the native payload, and advances
`latest` with a normal Git push. It does not rebuild or alter package files, execute
an installer, or connect to a device. Prereleases never replace this stable channel.
The device's existing PackageManager download/install preferences determine when
an available package is applied.

Repeated publication of identical bytes is a no-op. Downgrades, changed bytes for
an existing stable version, concurrent branch changes, unsafe archive paths and
missing evidence fail without force-pushing. Retry only after inspecting the
reported release and branch state. The manual **Publish SetupHelper package**
workflow recovers a missed post-release run; it accepts only the current latest
stable tag. Verification without publishing is also available locally:

```sh
python3 scripts/publish_setuphelper.py --tag v0.3.0
```

## Repository settings and dependency updates

After the workflows pass, Terraform enables `RELEASE_CHANNELS_ENABLED=true` and
`SETUPHELPER_PUBLICATION_ENABLED=true`. Stable promotion uses the protected
`release` environment with reviewers and a default-branch-only deployment policy.
The package publisher uses the repository `GITHUB_TOKEN` with `contents: write`
and `actions: read`; it needs no device or Google credentials. Preserve the main
branch's review/security rules and add **CI gate** only after its workflow lands.

Renovate replaces Dependabot. `renovate.json` extends the organization's atomic
CI preset so CodeQL init/analyze and other coupled action pins update together.
Explicit `pep621` and `dockerfile` managers retain Python/uv-lock and Docker updates.
The central toolkit's `renovate-repositories.json` must include this repository;
the existing bot runner supplies its own credentials. Dependency PRs still require
the same checks and review as application changes.
