#!/usr/bin/env bash
# Keep local validation and hosted CI on the same frozen dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."
# Third-party dependencies must have wheels. uv still permits this reviewed
# first-party project to be installed editable with its declared build backend.
uv sync --frozen --no-build
uv run --no-sync --no-build ruff check .
uv run --no-sync --no-build ruff format --check .
uv run --no-sync --no-build pytest
uv build
uv run --no-sync --no-build python scripts/version_plan.py check-base
uv run --no-sync --no-build python scripts/workflow_contracts.py
uv run --no-sync --no-build python -m unittest discover -s .github/release-tests -p 'test_*.py'
bash scripts/build-venus-bundle.sh
