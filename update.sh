#!/bin/sh
# Install the immutable payload, preserving private options and enabled state.
set -eu
package_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 -B "$package_dir/payload/deploy/venus/install.py" install \
    --bundle "$package_dir/payload" --root "${INVERTER_CLIMATE_ROOT:-/}"
