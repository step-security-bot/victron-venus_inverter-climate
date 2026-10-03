"""Publish verified stable package bytes to the branch downloaded by SetupHelper.

GitHub source tags contain developer sources, while PackageManager downloads a
branch archive. This adapter advances only the artifact branch, after the shared
toolkit has checked RC evidence, immutable source policy, receipts and checksums.
It never executes a packaged installer or connects to a Venus OS device.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from publish_verified import verified_assets
from release_control import GitHub, ReleaseError, require

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "latest"
PACKAGE = "inverter-climate"


def version_tuple(value: str) -> tuple[int, int, int]:
    require(
        bool(re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value)),
        "SetupHelper publication requires a stable package version",
    )
    return tuple(int(part) for part in value[1:].split("."))


def extract_package(archive: Path, destination: Path) -> Path:
    """Reject links, traversal, duplicate paths and oversized release archives."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        require(
            len(members) <= 10000 and sum(item.size for item in members) <= 100_000_000,
            "SetupHelper archive exceeds the size limit",
        )
        seen = set()
        for item in members:
            path = PurePosixPath(item.name)
            require(
                not path.is_absolute()
                and path.parts
                and path.parts[0] == PACKAGE
                and path.as_posix() == item.name.rstrip("/")
                and not any(part in ("..", ".git", ".github") for part in path.parts)
                and (item.isfile() or item.isdir())
                and path.as_posix() not in seen,
                "SetupHelper archive contains an unsafe or duplicate member",
            )
            seen.add(path.as_posix())
        bundle.extractall(destination, filter="data")
    package = destination / PACKAGE
    for name in ("setup", "version", "gitHubInfo", "pyproject.toml"):
        require((package / name).is_file(), "SetupHelper archive is missing package metadata")
    version_tuple((package / "version").read_text().strip())
    require(
        (package / "gitHubInfo").read_text().strip() == "victron-venus:latest",
        "SetupHelper package points to an unexpected update channel",
    )
    # Validate with reviewed publisher code, never import or execute the archive.
    spec = importlib.util.spec_from_file_location(
        "native_validator", ROOT / "deploy/venus/install.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.validate_bundle(package / "payload")
    return package


def git(directory: Path, *arguments: str, auth: dict | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=directory, env=auth, capture_output=True, text=True, check=False
    )
    require(result.returncode == 0, "SetupHelper Git operation failed; inspect branch state")
    return result.stdout.strip()


def publish_tree(
    package: Path,
    repository: str,
    tag: str,
    existing: dict | None,
    *,
    execute: bool,
    directory: Path,
) -> bool:
    """Keep artifact history and use a normal push, rejecting concurrent updates."""
    require(repository == "victron-venus/inverter-climate", "Unexpected package repository")
    require(
        (package / "version").read_text().strip() == tag,
        "Stable tag and packaged SetupHelper version differ",
    )
    token = os.environ.get("GH_TOKEN", "")
    require(bool(token), "GitHub token is required")
    auth = os.environ.copy()
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    auth.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    directory.mkdir()
    git(directory, "init", "-b", BRANCH)
    git(directory, "remote", "add", "origin", f"https://github.com/{repository}.git")
    if existing is not None:
        commit = existing.get("object", {})
        require(
            commit.get("type") == "commit"
            and bool(re.fullmatch(r"[0-9a-f]{40}", commit.get("sha", ""))),
            "SetupHelper branch must point to a commit",
        )
        git(directory, "fetch", "--no-tags", "origin", f"refs/heads/{BRANCH}", auth=auth)
        require(
            git(directory, "rev-parse", "FETCH_HEAD") == commit["sha"],
            "SetupHelper branch changed before publication",
        )
        git(directory, "reset", "--hard", commit["sha"])
        old_version = git(directory, "show", "HEAD:version")
        require(
            version_tuple(old_version) <= version_tuple(tag),
            "Refusing to downgrade the SetupHelper branch",
        )
    else:
        old_version = None
    for child in directory.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(package, directory, dirs_exist_ok=True)
    git(directory, "add", "--all")
    if existing is not None and not git(directory, "diff", "--cached", "--name-only"):
        print(f"SetupHelper {BRANCH} already contains {tag}")
        return False
    require(old_version != tag, "Refusing to replace package bytes for an existing stable version")
    if not execute:
        print(f"Verified {tag}; would advance SetupHelper {BRANCH}")
        return False
    git(
        directory,
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-m",
        f"Publish verified SetupHelper package {tag}",
    )
    git(directory, "push", "origin", f"HEAD:refs/heads/{BRANCH}", auth=auth)
    print(f"Published verified {tag} to SetupHelper {BRANCH}")
    return True


def publish(repository: str, requested_tag: str, *, execute: bool):
    gh = GitHub(repository)
    latest = gh.optional("releases/latest")
    if latest is None and not requested_tag:
        print("No stable release is available; SetupHelper publication skipped")
        return
    tag = requested_tag or latest["tag_name"]
    version_tuple(tag)
    require(
        latest is not None and tag == latest.get("tag_name"),
        "Only the current latest stable release may advance SetupHelper",
    )
    with TemporaryDirectory(prefix="setuphelper-publication-") as temporary:
        directory = Path(temporary)
        assets = directory / "assets"
        assets.mkdir()
        manifest = verified_assets(gh, tag, assets)
        archive = assets / f"inverter-climate-{manifest['version']}.tar.gz"
        require(archive.is_file(), "Verified release is missing its native package archive")
        package = extract_package(archive, directory / "extracted")
        current_latest = gh.api("releases/latest")
        require(
            current_latest.get("id") == latest.get("id") and current_latest.get("tag_name") == tag,
            "Latest stable release changed during verification",
        )
        existing = gh.optional(f"git/ref/heads/{BRANCH}")
        publish_tree(
            package, repository, tag, existing, execute=execute, directory=directory / "checkout"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="victron-venus/inverter-climate")
    parser.add_argument("--tag", default="")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        publish(args.repo, args.tag, execute=args.execute)
    except (ReleaseError, ValueError, OSError, tarfile.TarError):
        parser.exit(
            1, "SetupHelper publication failed; inspect release evidence and branch state\n"
        )


if __name__ == "__main__":
    main()
