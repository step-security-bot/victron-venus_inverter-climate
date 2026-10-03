"""Consumer-specific release contracts: archive safety and actual Git publication."""

import hashlib
import importlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
publisher = importlib.import_module("publish_setuphelper")
packager = importlib.import_module("package_release")
versions = importlib.import_module("version_plan")


def command(path, *args):
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()


def package_tree(path, version="v0.3.0"):
    path.mkdir()
    for name, content in {
        "setup": "#!/bin/sh\nexit 0\n",
        "version": version + "\n",
        "gitHubInfo": "victron-venus:latest\n",
        "pyproject.toml": '[project]\nname = "inverter-climate"\nversion = "0.3.0"\n',
        "payload/src/inverter_climate/service.py": "# release fixture\n",
        "payload/vendor/httpx/__init__.py": "# pure Python dependency\n",
        "payload/vendor/httpx.dist-info/WHEEL": "Tag: py3-none-any\n",
        "payload/deploy/venus/run": "#!/bin/sh\nexit 0\n",
        "payload/deploy/venus/log-run": "#!/bin/sh\nexit 0\n",
        "payload/deploy/venus/install.py": "# must never execute artifact code\n",
        "payload/deploy/venus/environment.example": "HA_TOKEN=\n",
        "payload/examples/venus.toml": '[service]\nmode = "observe"\n',
    }.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (path / "setup").chmod(0o755)
    payload = path / "payload"
    files = sorted(item for item in payload.rglob("*") if item.is_file())
    (payload / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(item.read_bytes()).hexdigest()}  "
            f"{item.relative_to(payload).as_posix()}\n"
            for item in files
        )
    )
    return path


def archive_tree(package, archive, extras=()):
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(package, arcname="inverter-climate")
        for item, data in extras:
            bundle.addfile(item, io.BytesIO(data))
    return archive


@pytest.fixture
def local_git(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    command(tmp_path, "init", "--bare", str(remote))
    original = publisher.git

    def redirected(directory, *arguments, auth=None):
        if arguments[:3] == ("remote", "add", "origin"):
            arguments = (*arguments[:3], str(remote))
        return original(directory, *arguments, auth=auth)

    monkeypatch.setattr(publisher, "git", redirected)
    monkeypatch.setenv("GH_TOKEN", "test-only-no-network")
    return remote


def branch_ref(remote):
    return {"object": {"type": "commit", "sha": command(remote, "rev-parse", "latest")}}


def publish(package, tmp_path, existing=None, execute=True, name="checkout"):
    return publisher.publish_tree(
        package,
        "victron-venus/inverter-climate",
        (package / "version").read_text().strip(),
        existing,
        execute=execute,
        directory=tmp_path / name,
    )


@pytest.mark.parametrize("value", ["0.3.0", "v0.3.0-rc.1", "v01.3.0", "v1.٣.0", "v1.2.3\n"])
def test_only_canonical_stable_versions_can_publish(value):
    with pytest.raises(publisher.ReleaseError):
        publisher.version_tuple(value)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("inverter-climate/../escape", tarfile.REGTYPE),
        ("/inverter-climate/absolute", tarfile.REGTYPE),
        ("another-package/file", tarfile.REGTYPE),
        ("inverter-climate/.git/config", tarfile.REGTYPE),
        ("inverter-climate/.github/workflows/bad.yml", tarfile.REGTYPE),
        ("inverter-climate/payload/link", tarfile.SYMTYPE),
        ("inverter-climate/payload/link", tarfile.LNKTYPE),
        ("inverter-climate//duplicate", tarfile.REGTYPE),
        ("inverter-climate/version", tarfile.REGTYPE),
        ("inverter-climate/payload/", tarfile.DIRTYPE),
    ],
)
def test_archive_rejects_unsafe_members_before_extraction(tmp_path, name, kind):
    package = package_tree(tmp_path / "source")
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = "/tmp/outside"
    archive = archive_tree(package, tmp_path / "unsafe.tar.gz", [(member, b"")])
    destination = tmp_path / "extracted"
    with pytest.raises(publisher.ReleaseError, match="unsafe or duplicate"):
        publisher.extract_package(archive, destination)
    assert not destination.exists()


def test_archive_checks_payload_with_trusted_validator(tmp_path):
    package = package_tree(tmp_path / "source")
    (package / "payload/vendor/httpx/__init__.py").write_text("# tampered\n")
    archive = archive_tree(package, tmp_path / "tampered.tar.gz")
    with pytest.raises(ValueError, match="checksum"):
        publisher.extract_package(archive, tmp_path / "extracted")


def test_archive_rejects_unexpected_channel(tmp_path):
    package = package_tree(tmp_path / "source")
    (package / "gitHubInfo").write_text("unrelated:main\n")
    archive = archive_tree(package, tmp_path / "wrong-channel.tar.gz")
    with pytest.raises(publisher.ReleaseError, match="unexpected update channel"):
        publisher.extract_package(archive, tmp_path / "extracted")


def test_rc_projected_archive_publishes_stable_bytes_and_preserves_executable(tmp_path, local_git):
    policy = json.loads((ROOT / ".release-policy.json").read_text())
    plan = versions.create_plan("0.3.0", "rc", 1, "a" * 40, policy)
    assert plan["tag"] == "v0.3.0-rc.1"
    assert versions.projections(plan)["package"] == "0.3.0"
    assert versions.projections(plan, "pep440")["package"] == "0.3.0"
    package = package_tree(tmp_path / "source", "v" + versions.projections(plan)["package"])
    archive = archive_tree(package, tmp_path / "inverter-climate-0.3.0.tar.gz")
    original_bytes = archive.read_bytes()
    extracted = publisher.extract_package(archive, tmp_path / "extracted")
    assert publish(extracted, tmp_path)
    assert command(local_git, "show", "latest:version") == "v0.3.0"
    assert (
        command(local_git, "show", "latest:payload/src/inverter_climate/service.py")
        == (package / "payload/src/inverter_climate/service.py").read_text().strip()
    )
    assert command(local_git, "ls-tree", "latest", "setup").startswith("100755")
    assert archive.read_bytes() == original_bytes
    assert not command(local_git, "ls-tree", "latest", ".github")


def test_current_policy_verifies_native_and_wheel_metadata(tmp_path):
    policy = json.loads((ROOT / ".release-policy.json").read_text())
    plan = versions.create_plan("0.3.0", "rc", 1, "a" * 40, policy)
    package = package_tree(tmp_path / "source")
    archive = archive_tree(package, tmp_path / "inverter-climate-0.3.0.tar.gz")
    wheel = tmp_path / "inverter_climate-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as distribution:
        distribution.writestr(
            "inverter_climate-0.3.0.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: inverter-climate\nVersion: 0.3.0\n",
        )
    declarations = policy["versioning"]["artifacts"]
    assert len(declarations) == 3
    for declaration in declarations:
        artifact = wheel if declaration["format"] == "wheel" else archive
        versions.verify_artifact(artifact, declaration, plan)


def test_publication_requires_shared_stable_verification_before_git(tmp_path, monkeypatch):
    class Releases:
        def __init__(self, repository):
            self.repo = repository

        def optional(self, endpoint):
            assert endpoint == "releases/latest"
            return {"tag_name": "v0.3.0", "id": 123}

    def failed_evidence(gh, tag, directory):
        assert gh.repo == "victron-venus/inverter-climate"
        assert tag == "v0.3.0" and directory.is_dir()
        raise publisher.ReleaseError("Source evidence rejected")

    monkeypatch.setattr(publisher, "GitHub", Releases)
    monkeypatch.setattr(publisher, "verified_assets", failed_evidence)
    monkeypatch.setattr(publisher, "git", lambda *_args, **_kw: pytest.fail("Unexpected Git write"))
    with pytest.raises(publisher.ReleaseError, match="evidence rejected"):
        publisher.publish("victron-venus/inverter-climate", "", execute=True)


def test_publication_is_idempotent_and_preserves_history(tmp_path, local_git):
    first = package_tree(tmp_path / "first")
    assert publish(first, tmp_path)
    before = branch_ref(local_git)
    assert not publish(first, tmp_path, before, name="retry")
    assert branch_ref(local_git) == before
    second = package_tree(tmp_path / "second", "v0.3.1")
    assert publish(second, tmp_path, before, name="upgrade")
    assert command(local_git, "rev-parse", "latest^") == before["object"]["sha"]


def test_stable_version_cannot_be_replaced_or_downgraded(tmp_path, local_git):
    package = package_tree(tmp_path / "source")
    publish(package, tmp_path)
    before = branch_ref(local_git)
    (package / "setup").write_text("#!/bin/sh\n# changed bytes\n")
    with pytest.raises(publisher.ReleaseError, match="replace package bytes"):
        publish(package, tmp_path, before, name="replacement")
    (package / "version").write_text("v0.2.0\n")
    with pytest.raises(publisher.ReleaseError, match="downgrade"):
        publish(package, tmp_path, before, name="downgrade")
    assert branch_ref(local_git) == before


def test_dry_run_never_creates_artifact_branch(tmp_path, local_git):
    assert not publish(package_tree(tmp_path / "source"), tmp_path, execute=False)
    assert command(local_git, "for-each-ref", "refs/heads/") == ""


def test_branch_change_since_observation_is_rejected(tmp_path, local_git):
    first = package_tree(tmp_path / "first")
    publish(first, tmp_path)
    before = branch_ref(local_git)
    second = package_tree(tmp_path / "second", "v0.3.1")
    publish(second, tmp_path, before, name="second-checkout")
    current = branch_ref(local_git)
    third = package_tree(tmp_path / "third", "v0.3.2")
    with pytest.raises(publisher.ReleaseError, match="changed before publication"):
        publish(third, tmp_path, before, name="stale-checkout")
    assert branch_ref(local_git) == current


def test_package_snapshot_excludes_untracked_operator_secrets(tmp_path, monkeypatch):
    source = tmp_path / "project"
    source.mkdir()
    command(source, "init")
    (source / "tracked").write_text("release input")
    (source / "ha-token").write_text("private operator file")
    command(source, "add", "tracked")
    monkeypatch.setattr(packager, "checked_version", lambda *_: "0.3.0")
    output = tmp_path / "out"
    calls = []

    def fake_build(arguments, *, cwd, check):
        assert check
        assert (cwd / "tracked").read_text() == "release input"
        assert not (cwd / "ha-token").exists()
        calls.append(arguments)
        if arguments[0] == "bash":
            Path(arguments[-1]).write_bytes(b"native bundle")
        else:
            (output / "inverter_climate-0.3.0-py3-none-any.whl").write_bytes(b"wheel")

    monkeypatch.setattr(packager.subprocess, "run", fake_build)
    # check_output uses subprocess.run too; leave Git inventory retrieval real.
    monkeypatch.setattr(packager.subprocess, "check_output", lambda *_args, **_kw: b"tracked\0")
    assets = packager.build_package(source, "0.3.0", "rc", output)
    assert len(calls) == len(assets) == 2
    assert len((output / "SHA256SUMS").read_text().splitlines()) == 2


def test_packaging_rejects_symlink_inputs_and_nonempty_output(tmp_path, monkeypatch):
    source = tmp_path / "project"
    source.mkdir()
    (source / "link").symlink_to(ROOT / "pyproject.toml")
    monkeypatch.setattr(packager, "checked_version", lambda *_: "0.3.0")
    monkeypatch.setattr(packager.subprocess, "check_output", lambda *_args, **_kw: b"link\0")
    with pytest.raises(ValueError, match="regular files"):
        packager.build_package(source, "0.3.0", "rc", tmp_path / "out")
    shutil.copy(ROOT / "version", tmp_path / "out/version")
    with pytest.raises(ValueError, match="must be empty"):
        packager.build_package(source, "0.3.0", "rc", tmp_path / "out")
