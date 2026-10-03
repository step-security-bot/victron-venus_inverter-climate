#!/usr/bin/env bash
# Keep local validation and hosted CI on the same frozen dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run python scripts/version_plan.py check-base
uv run python scripts/workflow_contracts.py
uv run python -m unittest discover -s .github/release-tests -p 'test_*.py'
bash scripts/build-venus-bundle.sh
