"""Build the frozen SetupHelper bundle and wheel in an isolated tracked checkout."""

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from release_version_adapter import checked_version


def build_package(root: Path, base: str, channel: str, output: Path) -> list[Path]:
    version = checked_version(root, base, channel)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Release output must be empty")
    # Credentials/operator files are never packaging inputs. Work from the Git
    # inventory, preserving the frozen overlay values already applied in CI.
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    with TemporaryDirectory(prefix="inverter-climate-release-") as directory:
        staged = Path(directory) / "project"
        staged.mkdir()
        for name in filter(None, tracked):
            source = root / name
            if source.is_symlink() or not source.is_file():
                raise ValueError("Release inputs must be tracked regular files")
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        archive = output / f"inverter-climate-{version}.tar.gz"
        subprocess.run(
            ["bash", "scripts/build-venus-bundle.sh", str(archive)], cwd=staged, check=True
        )
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(output)], cwd=staged, check=True)
    assets = sorted(output.glob("*.tar.gz")) + sorted(output.glob("*.whl"))
    if len(assets) != 2:
        raise ValueError("A release must contain exactly one native archive and one wheel")
    # The native builder also emits its standalone transport checksum file.
    assets.extend(sorted(output.glob("*.sha256")))
    checksums = []
    for path in assets:
        with path.open("rb") as stream:
            checksums.append(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {path.name}\n")
    (output / "SHA256SUMS").write_text("".join(checksums))
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("channel", choices=("nightly", "beta", "rc", "stable"))
    parser.add_argument("--output", type=Path, default=Path("release-dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for asset in build_package(root, args.version, args.channel, args.output.resolve()):
        print(asset)


if __name__ == "__main__":
    main()
