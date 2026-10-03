"""Exercise package entrypoints with the SetupHelper 9.3 lifecycle contract offline."""

import os
import shutil
import stat
import subprocess

import pytest
from test_venus_install import PROJECT, configure, installer, make_bundle

# Only the external helper is replaced. setup, update.sh, the payload installer,
# service registration and private files are the real package implementation.
# The device integration check uses the installed, unmodified SetupHelper 9.3.
HELPERS = r"""
scriptDir="$(cd "$(dirname "$0")" && pwd -P)"
packageName="$(basename "$scriptDir")"
setupOptionsDir="$INVERTER_CLIMATE_ROOT/data/setupOptions/$packageName"
installedVersionFile="$INVERTER_CLIMATE_ROOT/etc/venus/installedVersion-$packageName"
scriptAction=NONE
userInteraction=true
for argument in "$@"; do
    case "$argument" in
        install) scriptAction=INSTALL ;;
        uninstall) scriptAction=UNINSTALL ;;
        reinstall)
            if [ -f "$installedVersionFile" ] && \
                cmp -s "$scriptDir/version" "$installedVersionFile"; then
                exit 0
            fi
            scriptAction=INSTALL ;;
        runFromPm|auto) userInteraction=false ;;
    esac
done
mkdir -p "$setupOptionsDir"
logMessage() { printf '%s\n' "$*"; }
standardActionPrompt() { echo "Unexpected interactive prompt" >&2; exit 99; }
endScript() {
    mkdir -p "$(dirname "$installedVersionFile")"
    if [ "$scriptAction" = INSTALL ]; then
        touch "$setupOptionsDir/optionsSet"
        rm -f "$setupOptionsDir/DO_NOT_AUTO_INSTALL"
        cp "$scriptDir/version" "$installedVersionFile"
    elif [ "$scriptAction" = UNINSTALL ]; then
        touch "$setupOptionsDir/DO_NOT_AUTO_INSTALL"
        rm -f "$installedVersionFile"
    fi
    exit 0
}
"""


def package(path, version):
    path.mkdir(parents=True)
    for name in ("setup", "update.sh", "gitHubInfo", "GUI_V1_NOT_REQUIRED", "packageDependencies"):
        shutil.copy2(PROJECT / name, path / name)
    (path / "version").write_text(version + "\n")
    make_bundle(path / "payload", version)
    return path


@pytest.fixture
def device(tmp_path):
    root = tmp_path / "device"
    helpers = root / "data/SetupHelper/HelperResources/IncludeHelpers"
    helpers.parent.mkdir(parents=True)
    helpers.write_text(HELPERS)
    source = package(root / "data/inverter-climate", "v0.3.0")
    return root, source, installer.Installer(root)


def setup_action(root, source, *arguments, success=True):
    result = subprocess.run(
        ["bash", str(source / "setup"), *arguments],
        input="",
        capture_output=True,
        text=True,
        env={**os.environ, "INVERTER_CLIMATE_ROOT": str(root)},
        check=False,
    )
    if success:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    return result


def test_package_metadata_matches_setuphelper_discovery_contract():
    assert (PROJECT / "version").read_text().startswith("v")
    assert (PROJECT / "gitHubInfo").read_text().strip() == "victron-venus:latest"
    assert (PROJECT / "GUI_V1_NOT_REQUIRED").is_file()
    assert (PROJECT / "packageDependencies").read_text().strip() == "SetupHelper installed"
    # Configuration is needed for explicit start, not for a disabled installation.
    assert not (PROJECT / "optionsRequired").exists()


def test_three_setuphelper_cycles_preserve_private_data_and_runtime(device):
    root, source, native = device
    setup_action(root, source, "install", "runFromPm")
    assert stat.S_IMODE(native.options.stat().st_mode) == 0o700
    configure(native)
    state = native.path("/data/inverter-climate-state/state.json")
    state.write_text('{"owner": "retained-private-fixture"}\n')
    private = {
        path: path.read_bytes()
        for path in (native.options / "config.toml", native.options / "environment", state)
    }
    installed = root / "etc/venus/installedVersion-inverter-climate"
    for cycle in range(3):
        native.start()
        version = f"v0.3.{cycle + 1}"
        # This is PackageManager's destructive source replacement. The running
        # bundle and rollback target must both live elsewhere.
        shutil.rmtree(source)
        package(source, version)
        (source / "patchErrors").write_text("SetupHelper-owned mutable metadata\n")
        setup_action(root, source, "install", "runFromPm")
        assert installed.read_text().strip() == version
        assert version in (native.current / "src/inverter_climate/service.py").read_text()
        assert native.previous.is_dir()
        assert not (native.definition / "down").exists()
        assert (native.options / "optionsSet").is_file()
        setup_action(root, source, "uninstall", "runFromPm")
        assert not native.link.exists()
        assert not installed.exists()
        assert (native.options / "DO_NOT_AUTO_INSTALL").is_file()
        assert installer.START not in native.rc.read_text()
        setup_action(root, source, "reinstall", "runFromPm")
        assert native.link.is_symlink()
        assert (native.definition / "down").is_file()
        assert not (native.options / "DO_NOT_AUTO_INSTALL").exists()
        assert installed.read_text().strip() == version
        for path, expected in private.items():
            assert path.read_bytes() == expected


def test_firmware_reinstall_restores_enabled_service_without_private_changes(device):
    root, source, native = device
    setup_action(root, source, "install", "auto")
    configure(native)
    native.start()
    inode = native.current.stat().st_ino
    setup_action(root, source, "reinstall", "auto")
    assert native.current.stat().st_ino == inode  # SetupHelper's same-version fast path.
    (root / "etc/venus/installedVersion-inverter-climate").unlink()
    native.link.unlink()  # Firmware update recreated /etc and /service.
    private = (native.options / "environment").read_bytes()
    setup_action(root, source, "reinstall", "auto")
    assert native.link.is_symlink()
    assert not (native.definition / "down").exists()
    assert (native.options / "environment").read_bytes() == private


def test_failed_payload_update_does_not_mark_version_installed_or_stop_service(device):
    root, source, native = device
    setup_action(root, source, "install", "runFromPm")
    configure(native)
    native.start()
    (source / "version").write_text("v0.4.0\n")
    (source / "payload/src/inverter_climate/service.py").write_text("tampered\n")
    setup_action(root, source, "install", "runFromPm", success=False)
    assert (root / "etc/venus/installedVersion-inverter-climate").read_text() == "v0.3.0\n"
    assert native.link.is_symlink()
    assert not (native.definition / "down").exists()
    assert not (native.options / "DO_NOT_AUTO_INSTALL").exists()


def test_missing_action_or_helper_exits_without_prompting_or_installing(device):
    root, source, native = device
    result = setup_action(root, source, success=False)
    assert "explicit setup action" in result.stderr
    assert not native.current.exists()
    shutil.rmtree(root / "data/SetupHelper")
    result = setup_action(root, source, "install", "runFromPm", success=False)
    assert "SetupHelper must be installed" in result.stderr
    assert not native.current.exists()


def test_setup_refuses_source_outside_canonical_package_path(device, tmp_path):
    root, _source, native = device
    candidate = package(tmp_path / "staging", "v0.3.0")
    result = setup_action(root, candidate, "install", "auto", success=False)
    assert "verified package" in result.stderr
    assert not native.current.exists()


def test_offline_root_accepts_filesystem_alias_for_canonical_source(device, tmp_path):
    root, _source, native = device
    alias = tmp_path / "device-alias"
    alias.symlink_to(root, target_is_directory=True)
    setup_action(alias, alias / "data/inverter-climate", "install", "runFromPm")
    assert native.current.is_dir()
    assert native.link.is_symlink()
