#!/usr/bin/env python3
"""Install one native Venus OS service; --root runs against an offline filesystem."""

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

START = "# === inverter-climate service persistence ==="
END = "# === end inverter-climate ==="
BOOT = START + "\npython3 -B /data/inverter-climate/deploy/venus/install.py boot\n" + END + "\n"
MARKER = "inverter-climate-native-v1\n"


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".inverter-climate-", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def persistence(text, enabled):
    """Replace only our complete marked block; preserve the rest byte-for-byte."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == START]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise ValueError("Ambiguous inverter-climate rc.local markers; inspect before updating")
        del lines[starts[0] : ends[0] + 1]
    if not enabled:
        return "".join(lines)
    # Place the hook before a terminal exit, without moving the shebang or
    # inserting inside an unrelated conditional service block.
    meaningful = [
        i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")
    ]
    insertion = len(lines)
    if meaningful and re.fullmatch(
        r"[ \t]*exit[ \t]+0[ \t]*;?[ \t]*(?:#[^\n]*)?(?:\r?\n)?", lines[meaningful[-1]]
    ):
        insertion = meaningful[-1]
    if insertion and not lines[insertion - 1].endswith("\n"):
        lines[insertion - 1] += "\n"
    lines.insert(insertion, BOOT)
    return "".join(lines)


def validate_bundle(bundle):
    """Require the exact checksummed payload, regular files, and pure Python wheels."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("Bundle must be an ordinary directory")
    manifest = bundle / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise ValueError("A checksummed native bundle is required")
    expected = {}
    for line in manifest.read_text().splitlines():
        digest, name = line.split("  ", 1)
        relative = Path(name)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", digest)
            or relative.is_absolute()
            or ".." in relative.parts
            or name in expected
            or name == "SHA256SUMS"
        ):
            raise ValueError("Invalid bundle manifest")
        expected[name] = digest
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("Bundle must contain only ordinary files and directories")
        if not path.is_file() or path == manifest:
            continue
        name = path.relative_to(bundle).as_posix()
        actual.add(name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected.get(name):
            raise ValueError("Bundle checksum mismatch")
        magic = path.read_bytes()[:4]
        if (
            path.suffix in {".so", ".pyd", ".dylib", ".pyc"}
            or magic
            in {
                b"\x7fELF",
                b"\xfe\xed\xfa\xce",
                b"\xfe\xed\xfa\xcf",
                b"\xcf\xfa\xed\xfe",
            }
            or magic[:2] == b"MZ"
        ):
            raise ValueError("Native binaries and bytecode are not portable Venus dependencies")
    if actual != expected.keys():
        raise ValueError("Bundle files do not match the manifest")
    required = {
        "src/inverter_climate/service.py",
        "vendor/httpx/__init__.py",
        "deploy/venus/run",
        "deploy/venus/log-run",
        "deploy/venus/install.py",
        "deploy/venus/environment.example",
        "examples/venus.toml",
    }
    if not required <= actual:
        raise ValueError("Bundle is missing runtime files")
    wheels = list((bundle / "vendor").glob("*.dist-info/WHEEL"))
    if not wheels or any(
        not (tags := re.findall(r"^Tag: (.+)$", path.read_text(), re.MULTILINE))
        or any(not tag.endswith("-none-any") for tag in tags)
        for path in wheels
    ):
        raise ValueError("Only architecture-independent pure Python wheels are supported")


class Installer:
    def __init__(self, root):
        self.root = root.resolve()
        self.offline = self.root != Path("/")
        self.current = self.path("/data/inverter-climate")
        self.previous = self.path("/data/inverter-climate.previous")
        self.options = self.path("/data/setupOptions/inverter-climate")
        self.definition = self.options / "service"
        self.link = self.path("/service/inverter-climate")
        self.rc = self.path("/data/rc.local")

    def path(self, absolute):
        return self.root / absolute.lstrip("/")

    def check_ownership(self):
        if os.path.lexists(self.link) and (
            not self.link.is_symlink() or os.readlink(self.link) != str(self.definition)
        ):
            raise ValueError("Refusing to replace a service path owned by another installation")
        if self.definition.is_symlink() or (
            self.definition.exists()
            and (
                not (self.definition / ".owner").is_file()
                or (self.definition / ".owner").read_text() != MARKER
            )
        ):
            raise ValueError("Refusing to replace an unrecognized persistent service definition")
        if self.rc.is_symlink():
            raise ValueError("Refusing to replace a symlinked rc.local")
        persistence(self.rc.read_text() if self.rc.exists() else "#!/bin/sh\n", False)

    def stop(self, *, retire=False):
        if not os.path.lexists(self.link) or self.offline:
            return
        subprocess.run(["svc", "-d", str(self.link)], check=True, capture_output=True)
        for attempt in range(60):
            result = subprocess.run(["svstat", str(self.link)], capture_output=True, text=True)
            if result.returncode == 0 and ": down " in result.stdout:
                break
            if attempt == 40:
                subprocess.run(["svc", "-k", str(self.link)], check=True, capture_output=True)
            time.sleep(0.5)
        else:
            raise RuntimeError("Service did not stop; bundle was not replaced")
        if retire:
            for service in (self.link / "log", self.link):
                subprocess.run(["svc", "-dx", str(service)], check=True, capture_output=True)

    def register(self, enabled):
        self.definition.mkdir(parents=True, exist_ok=True)
        atomic_write(self.definition / ".owner", MARKER)
        for source, destination in (("run", "run"), ("log-run", "log/run")):
            atomic_write(
                self.definition / destination,
                (self.current / "deploy/venus" / source).read_text(),
                0o755,
            )
        down = self.definition / "down"
        if enabled:
            down.unlink(missing_ok=True)
        else:
            atomic_write(down, "")
        self.link.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(self.link):
            self.link.symlink_to(self.definition)
        content = self.rc.read_text() if self.rc.exists() else "#!/bin/sh\n"
        mode = stat.S_IMODE(self.rc.stat().st_mode) if self.rc.exists() else 0o755
        atomic_write(self.rc, persistence(content, True), mode | 0o100)
        runtime = self.path("/run/inverter-climate")
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if enabled and not self.offline:
            # svscan creates the supervisor after discovering a new symlink.
            for _attempt in range(30):
                result = subprocess.run(["svc", "-u", str(self.link)], capture_output=True)
                if result.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Service registered but supervisor is not ready")

    def install(self, bundle, start):
        validate_bundle(bundle)
        self.check_ownership()
        for existing in (self.current, self.previous):
            if existing.exists():
                if existing.is_symlink():
                    raise ValueError("Refusing to replace a symlinked bundle")
                validate_bundle(existing)
        if bundle.resolve() in (self.current, self.previous):
            raise ValueError("Install from a separate extracted bundle")
        enabled = start or (self.definition.exists() and not (self.definition / "down").exists())
        if enabled and not all(
            (self.options / name).is_file() for name in ("config.toml", "environment")
        ):
            raise ValueError("Configure private config.toml and environment before --start")
        if not self.offline:
            if sys.version_info < (3, 12):  # noqa: UP036 - executed directly on target firmware
                raise ValueError("Venus OS must provide Python 3.12 or newer")
            code = (
                "import sys, dbus, tomllib; sys.path[:0] = sys.argv[1:]; "
                "import httpx, inverter_climate.service"
            )
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    code,
                    str(bundle / "src"),
                    str(bundle / "vendor"),
                ],
                check=True,
                capture_output=True,
            )
        self.current.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".inverter-climate-stage-", dir=self.current.parent))
        try:
            shutil.copytree(bundle, staged, dirs_exist_ok=True)
            validate_bundle(staged)
            self.stop()
            if self.previous.exists():
                shutil.rmtree(self.previous)
            if self.current.exists():
                self.current.rename(self.previous)
            staged.rename(self.current)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        self.options.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path("/data/inverter-climate-state").mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(
            self.options / "environment.example",
            (self.current / "deploy/venus/environment.example").read_text(),
        )
        atomic_write(
            self.options / "config.example.toml",
            (self.current / "examples/venus.toml").read_text(),
        )
        self.register(enabled)

    def rollback(self):
        self.check_ownership()
        validate_bundle(self.current)
        validate_bundle(self.previous)
        enabled = self.definition.exists() and not (self.definition / "down").exists()
        self.stop()
        temporary = self.path("/data/.inverter-climate-rollback")
        if temporary.exists():
            raise ValueError("A previous rollback is incomplete; inspect its temporary directory")
        self.current.rename(temporary)
        self.previous.rename(self.current)
        temporary.rename(self.previous)
        self.register(enabled)

    def start(self):
        self.check_ownership()
        validate_bundle(self.current)
        if not all((self.options / name).is_file() for name in ("config.toml", "environment")):
            raise ValueError("Configure private config.toml and environment before starting")
        self.register(True)

    def uninstall(self):
        self.check_ownership()
        if self.definition.exists():
            atomic_write(self.definition / "down", "")
        self.stop(retire=True)
        self.link.unlink(missing_ok=True)
        if self.rc.exists():
            atomic_write(
                self.rc,
                persistence(self.rc.read_text(), False),
                stat.S_IMODE(self.rc.stat().st_mode),
            )

    def boot(self):
        self.check_ownership()
        if not self.current.is_dir() or not self.definition.is_dir():
            raise ValueError("Native package/service definition is missing")
        self.path("/run/inverter-climate").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.link.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(self.link):
            self.link.symlink_to(self.definition)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=("install", "start", "rollback", "uninstall", "boot"),
        default="install",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help="Offline test filesystem; never runs supervisor commands",
    )
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--start",
        action="store_true",
        help="Explicitly enable the configured service after installing",
    )
    args = parser.parse_args()
    installer = Installer(args.root)
    if not installer.offline and os.geteuid() != 0:
        parser.error("Real-device installation requires root")
    try:
        if args.action == "install":
            installer.install(args.bundle.resolve(), args.start)
        else:
            if args.start:
                parser.error("--start is only valid for install")
            getattr(installer, args.action)()
        print(
            f"inverter-climate native {args.action} complete; "
            "private configuration and journal preserved"
        )
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            f"Native {args.action} failed: {type(error).__name__}; "
            "inspect bundle, service ownership and prerequisites",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
