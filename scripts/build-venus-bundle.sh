#!/usr/bin/env bash
# Build on a development host. Never install dependencies into the device Python.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
output_path="${1:-$project_dir/dist/inverter-climate-venus.tar.gz}"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
cd "$project_dir"
uv export --frozen --no-dev --no-emit-project --no-header \
  --output-file "$build_dir/requirements.txt" > /dev/null
mkdir -p "$build_dir/inverter-climate/payload/vendor"
# Linux markers and Python 3.12 are deliberate even on a macOS build host.
# Reject every non-pure wheel after resolution, so the output also runs on ARMv7.
uv pip install --python-version 3.12 --python-platform aarch64-unknown-linux-gnu \
  --target "$build_dir/inverter-climate/payload/vendor" --only-binary :all: \
  --require-hashes --no-deps --requirements "$build_dir/requirements.txt"
python3 - "$project_dir" "$build_dir" "$output_path" <<'PY'
import hashlib
import importlib.util
import json
import shutil
import sys
import tarfile
from pathlib import Path

sys.dontwrite_bytecode = True
project, temporary, output = map(Path, sys.argv[1:])
package = temporary / "inverter-climate"
bundle = package / "payload"
for name in ("setup", "update.sh", "gitHubInfo", "version", "pyproject.toml", "GUI_V1_NOT_REQUIRED", "packageDependencies"):
    shutil.copy2(project / name, package / name)
shutil.copytree(project / "src", bundle / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copytree(project / "deploy/venus", bundle / "deploy/venus", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
(bundle / "examples").mkdir()
shutil.copyfile(project / "examples/venus.toml", bundle / "examples/venus.toml")
for name in ("README.md", "LICENSE"):
    shutil.copyfile(project / name, package / name)
shutil.copyfile(project / "version", bundle / "version")
shutil.copyfile(temporary / "requirements.txt", bundle / "requirements.txt")
shutil.rmtree(bundle / "vendor/bin", ignore_errors=True)
for path in (bundle / "vendor").rglob("__pycache__"):
    shutil.rmtree(path)
if (bundle / "vendor/dbus").exists() or list((bundle / "vendor").glob("*dbus*")):
    raise SystemExit("dbus must be supplied by Venus OS firmware, never bundled")
metadata = {
    "format": 1, "python": ">=3.12", "platform": "pure-python",
    "firmware_dependencies": ["dbus", "dbus.mainloop.glib", "gi.repository.GLib", "velib_python"],
}
(bundle / "BUNDLE.json").write_text(json.dumps(metadata, indent=2) + "\n")
files = sorted(path for path in bundle.rglob("*") if path.is_file())
(bundle / "SHA256SUMS").write_text("".join(
    hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.relative_to(bundle).as_posix() + "\n"
    for path in files
))
spec = importlib.util.spec_from_file_location("venus_installer", project / "deploy/venus/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
installer.validate_bundle(bundle)
output = output.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(output, "w:gz") as archive:
    for path in sorted(package.rglob("*")):
        info = archive.gettarinfo(str(path), "inverter-climate/" + path.relative_to(package).as_posix())
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = 0
        if path.is_file():
            with path.open("rb") as content:
                archive.addfile(info, content)
        else:
            archive.addfile(info)
checksum = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_name(output.name + ".sha256").write_text(checksum + "  " + output.name + "\n")
print(f"Built and validated pure Python Venus bundle: {output}")
PY
