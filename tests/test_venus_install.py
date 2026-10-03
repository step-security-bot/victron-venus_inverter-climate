"""Offline native installation lifecycle; no real service or device is touched."""

import hashlib
import importlib.util
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("venus_install", PROJECT / "deploy/venus/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def manifest(bundle):
    files = sorted(
        path for path in bundle.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    (bundle / "SHA256SUMS").write_text(
        "".join(
            hashlib.sha256(path.read_bytes()).hexdigest()
            + "  "
            + path.relative_to(bundle).as_posix()
            + "\n"
            for path in files
        )
    )


def make_bundle(path, version="first"):
    path.mkdir()
    shutil.copytree(
        PROJECT / "deploy/venus",
        path / "deploy/venus",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name, content in {
        "examples/venus.toml": '[service]\nmode = "observe"\n',
        "src/inverter_climate/service.py": f"VERSION = {version!r}\n",
        "vendor/httpx/__init__.py": "# portable test fixture\n",
        "vendor/httpx.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    manifest(path)
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    native = installer.Installer(tmp_path / "device")

    def no_commands(*args, **kwargs):
        pytest.fail("An offline installation attempted to call a device command")

    monkeypatch.setattr(installer.subprocess, "run", no_commands)
    return native, make_bundle(tmp_path / "bundle")


def configure(native):
    native.options.mkdir(parents=True, exist_ok=True)
    (native.options / "config.toml").write_text('mode = "observe"\n')
    (native.options / "environment").write_text("HA_TOKEN=private-example\n")


def test_initial_install_is_disabled_and_creates_only_owned_link(setup):
    native, bundle = setup
    native.install(bundle, False)
    assert native.link.is_symlink()
    assert native.link.readlink() == native.definition
    assert (native.definition / "down").is_file()
    assert stat.S_IMODE((native.definition / "run").stat().st_mode) == 0o755
    assert stat.S_IMODE(native.options.stat().st_mode) == 0o700
    assert native.rc.read_text().startswith("#!/bin/sh\n")
    assert not (native.options / "config.toml").exists()
    assert not (native.options / "environment").exists()


def test_update_rollback_and_uninstall_preserve_private_data_and_service_inode(setup, tmp_path):
    native, first = setup
    native.install(first, False)
    configure(native)
    state = native.path("/data/inverter-climate-state/state.json")
    state.write_text('{"pending": "private-example"}')
    inode = native.definition.stat().st_ino
    native.start()
    assert not (native.definition / "down").exists()
    second = make_bundle(tmp_path / "second", "second")
    native.install(second, False)
    assert "second" in (native.current / "src/inverter_climate/service.py").read_text()
    assert "first" in (native.previous / "src/inverter_climate/service.py").read_text()
    assert native.definition.stat().st_ino == inode
    assert not (native.definition / "down").exists()
    native.rollback()
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert "second" in (native.previous / "src/inverter_climate/service.py").read_text()
    native.uninstall()
    native.uninstall()
    assert not native.link.is_symlink()
    assert installer.START not in native.rc.read_text()
    assert (native.definition / "down").is_file()
    assert state.read_text() == '{"pending": "private-example"}'
    assert (native.options / "environment").read_text() == "HA_TOKEN=private-example\n"
    assert (native.options / "config.toml").read_text() == 'mode = "observe"\n'


def test_boot_recreates_only_own_service_link_without_enabling_down_service(setup):
    native, bundle = setup
    native.install(bundle, False)
    native.link.unlink()
    native.boot()
    native.boot()
    assert native.link.readlink() == native.definition
    assert (native.definition / "down").is_file()


@pytest.mark.parametrize("ending", ["exit 0\n", "exit 0;\n", "exit 0 # finished\n"])
def test_rc_local_preserves_shebang_other_blocks_and_terminal_exit(ending):
    original = "#!/bin/sh\n# Other service\nif true; then\n  echo existing\nfi\n" + ending
    added = installer.persistence(original, True)
    assert added.startswith("#!/bin/sh\n")
    assert added.index(installer.BOOT) < added.index("exit 0")
    assert added.count(installer.START) == 1
    assert installer.persistence(added, True) == added
    assert installer.persistence(added, False) == original


@pytest.mark.parametrize("text", [installer.START + "\n", installer.END + "\n", installer.BOOT * 2])
def test_ambiguous_boot_markers_are_rejected_without_silent_deletion(text):
    with pytest.raises(ValueError, match="Ambiguous"):
        installer.persistence(text, True)


@pytest.mark.parametrize("foreign", ["directory", "symlink", "broken_symlink"])
def test_foreign_service_path_is_not_replaced(setup, tmp_path, foreign):
    native, bundle = setup
    native.link.parent.mkdir(parents=True)
    if foreign == "directory":
        native.link.mkdir()
    else:
        target = tmp_path / "another-service"
        if foreign == "symlink":
            target.mkdir()
        native.link.symlink_to(target)
    with pytest.raises(ValueError, match="another installation"):
        native.install(bundle, False)
    assert native.link.exists() or native.link.is_symlink()
    assert not native.current.exists()


def test_start_requires_both_private_files(setup):
    native, bundle = setup
    with pytest.raises(ValueError, match="private"):
        native.install(bundle, True)
    assert not native.current.exists()
    native.install(bundle, False)
    with pytest.raises(ValueError, match="private"):
        native.start()
    assert (native.definition / "down").is_file()


@pytest.mark.parametrize(
    "mutation", ["tamper", "extra", "missing", "symlink", "native_wheel", "binary"]
)
def test_invalid_bundle_is_rejected_before_existing_service_changes(setup, tmp_path, mutation):
    native, bundle = setup
    native.install(bundle, False)
    configure(native)
    native.start()
    before = native.rc.read_bytes()
    candidate = make_bundle(tmp_path / "candidate", "bad")
    source = candidate / "src/inverter_climate/service.py"
    if mutation == "tamper":
        source.write_text("tampered")
    elif mutation == "extra":
        (candidate / "extra").write_text("unexpected")
    elif mutation == "missing":
        source.unlink()
    elif mutation == "symlink":
        source.unlink()
        source.symlink_to(bundle / "src/inverter_climate/service.py")
    elif mutation == "native_wheel":
        (candidate / "vendor/httpx.dist-info/WHEEL").write_text("Tag: cp312-cp312-linux_x86_64\n")
        manifest(candidate)
    else:
        (candidate / "vendor/native").write_bytes(b"\x7fELFfake")
        manifest(candidate)
    with pytest.raises(ValueError):
        native.install(candidate, False)
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert not (native.definition / "down").exists()
    assert native.rc.read_bytes() == before


def test_existing_unrecognized_bundle_is_not_removed(setup):
    native, bundle = setup
    native.current.mkdir(parents=True)
    old = native.current / "operator-file"
    old.write_text("preserve")
    with pytest.raises(ValueError, match="checksummed"):
        native.install(bundle, False)
    assert old.read_text() == "preserve"


def test_cli_root_runs_without_root_or_device_tools(tmp_path):
    bundle = make_bundle(tmp_path / "bundle")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT / "deploy/venus/install.py"),
            "install",
            "--root",
            str(tmp_path / "offline"),
            "--bundle",
            str(bundle),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "install complete" in result.stdout


def test_launchers_use_firmware_python_and_volatile_logs():
    run = (PROJECT / "deploy/venus/run").read_text()
    assert "exec python3 -m inverter_climate.service" in run
    assert "/data/setupOptions/inverter-climate/environment" in run
    assert "PYTHONDONTWRITEBYTECODE=1" in run
    assert "pip " not in run
    assert (
        "multilog t s65536 n2 /run/inverter-climate/log"
        in (PROJECT / "deploy/venus/log-run").read_text()
    )


def test_live_update_checks_firmware_then_stops_before_replacing_bundle(
    setup, tmp_path, monkeypatch
):
    native, first = setup
    native.install(first, False)
    configure(native)
    native.start()
    native.offline = False
    commands = []

    def supervisor(command, **kwargs):
        commands.append(command)
        if command[0] == "svc" and command[1] == "-d":
            assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
        if command[0] == "svc" and command[1] == "-u":
            assert "second" in (native.current / "src/inverter_climate/service.py").read_text()
        return subprocess.CompletedProcess(command, 0, stdout=f"{native.link}: down 1 seconds\n")

    monkeypatch.setattr(installer.subprocess, "run", supervisor)
    native.install(make_bundle(tmp_path / "second", "second"), False)
    assert commands[0][1:4] == ["-I", "-B", "-c"]
    assert "import sys, dbus, tomllib" in commands[0][4]
    assert [(command[0], command[1]) for command in commands[1:]] == [
        ("svc", "-d"),
        ("svstat", str(native.link)),
        ("svc", "-u"),
    ]


def test_failed_firmware_preflight_keeps_existing_package_running(setup, tmp_path, monkeypatch):
    native, first = setup
    native.install(first, False)
    configure(native)
    native.start()
    native.offline = False
    commands = []

    def failure(command, **kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(installer.subprocess, "run", failure)
    with pytest.raises(subprocess.CalledProcessError):
        native.install(make_bundle(tmp_path / "second", "second"), False)
    assert len(commands) == 1
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert not native.previous.exists()
    assert not (native.definition / "down").exists()


@pytest.mark.parametrize("prior_enabled", [False, True])
@pytest.mark.parametrize("failed_rename", ["save_current", "promote_staged"])
def test_failed_promotion_restores_current_and_prior_service_state(
    setup, tmp_path, monkeypatch, prior_enabled, failed_rename
):
    native, first = setup
    native.install(first, False)
    configure(native)
    if prior_enabled:
        native.start()
    original_rename = Path.rename
    registered = []
    original_register = native.register

    def register(enabled):
        registered.append(enabled)
        original_register(enabled)

    def fail(source, target):
        if failed_rename == "save_current" and source == native.current:
            raise OSError("simulated first rename failure")
        if failed_rename == "promote_staged" and source.name.startswith(".inverter-climate-stage-"):
            raise OSError("simulated promotion failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail)
    monkeypatch.setattr(native, "register", register)
    with pytest.raises(OSError):
        native.install(make_bundle(tmp_path / "candidate", "candidate"), True)
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert (native.definition / "down").exists() is not prior_enabled
    assert registered == [prior_enabled]
    assert not list(native.current.parent.glob(".inverter-climate-stage-*"))


def test_failed_promotion_and_failed_recovery_retain_previous_for_explicit_rollback(
    setup, tmp_path, monkeypatch
):
    native, first = setup
    native.install(first, False)
    configure(native)
    native.start()
    original_rename = Path.rename

    def fail(source, target):
        if source.name.startswith(".inverter-climate-stage-") or source == native.previous:
            raise OSError("simulated promotion and restore failures")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(RuntimeError, match="recovery is incomplete"):
        native.install(make_bundle(tmp_path / "candidate", "candidate"), False)
    assert not native.current.exists()
    assert "first" in (native.previous / "src/inverter_climate/service.py").read_text()
    assert len(list(native.current.parent.glob(".inverter-climate-stage-*"))) == 1
    monkeypatch.setattr(Path, "rename", original_rename)
    native.rollback()
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert not (native.definition / "down").exists()


def test_install_refuses_to_delete_only_good_previous_after_interruption(setup, tmp_path):
    native, first = setup
    native.install(first, False)
    native.current.rename(native.previous)
    with pytest.raises(ValueError, match="explicit rollback"):
        native.install(make_bundle(tmp_path / "candidate", "candidate"), False)
    assert "first" in (native.previous / "src/inverter_climate/service.py").read_text()
    native.rollback()
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert (native.definition / "down").exists()


def test_promotion_fsync_failure_after_rename_restores_previous_version(
    setup, tmp_path, monkeypatch
):
    native, first = setup
    native.install(first, False)
    configure(native)
    native.start()
    original_move = installer.move_bundle

    def fail_after_rename(source, target):
        original_move(source, target)
        if source.name.startswith(".inverter-climate-stage-"):
            raise OSError("simulated directory fsync failure after rename")

    monkeypatch.setattr(installer, "move_bundle", fail_after_rename)
    with pytest.raises(OSError):
        native.install(make_bundle(tmp_path / "candidate", "candidate"), False)
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert "candidate" in (native.previous / "src/inverter_climate/service.py").read_text()
    assert not (native.definition / "down").exists()
    assert not native.rollback_temporary.exists()


@pytest.mark.parametrize("step", [1, 2, 3])
def test_rollback_rename_failure_keeps_current_or_explicitly_resumable_swap(
    setup, tmp_path, monkeypatch, step
):
    native, first = setup
    native.install(first, False)
    configure(native)
    native.start()
    native.install(make_bundle(tmp_path / "second", "second"), False)
    original_rename = Path.rename
    count = 0

    def fail(source, target):
        nonlocal count
        count += 1
        if count == step:
            raise OSError("simulated rollback rename failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError):
        native.rollback()
    assert native.current.is_dir()
    assert not (native.definition / "down").exists()
    if step in (1, 2):
        assert "second" in (native.current / "src/inverter_climate/service.py").read_text()
        assert "first" in (native.previous / "src/inverter_climate/service.py").read_text()
        assert not native.rollback_temporary.exists()
    else:
        assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
        assert (
            "second" in (native.rollback_temporary / "src/inverter_climate/service.py").read_text()
        )
        assert not native.previous.exists()
    native.rollback()
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert "second" in (native.previous / "src/inverter_climate/service.py").read_text()
    assert not native.rollback_temporary.exists()


@pytest.mark.parametrize("completed_renames", [1, 2])
def test_explicit_rollback_resumes_interrupted_swap_without_toggling_again(
    setup, tmp_path, completed_renames
):
    native, first = setup
    native.install(first, False)
    native.install(make_bundle(tmp_path / "second", "second"), False)
    native.current.rename(native.rollback_temporary)
    if completed_renames == 2:
        native.previous.rename(native.current)
    native.rollback()
    assert "first" in (native.current / "src/inverter_climate/service.py").read_text()
    assert "second" in (native.previous / "src/inverter_climate/service.py").read_text()
    assert not native.rollback_temporary.exists()
    assert (native.definition / "down").exists()


def test_ambiguous_rollback_preserves_every_copy_without_stopping(setup, tmp_path, monkeypatch):
    native, first = setup
    native.install(first, False)
    native.install(make_bundle(tmp_path / "second", "second"), False)
    shutil.copytree(first, native.rollback_temporary)
    monkeypatch.setattr(native, "stop", lambda: pytest.fail("ambiguous recovery stopped service"))
    with pytest.raises(ValueError, match="Ambiguous"):
        native.rollback()
    assert (
        native.current.is_dir() and native.previous.is_dir() and native.rollback_temporary.is_dir()
    )
