#!/usr/bin/env bash
# scan-prs.sh — scan upstream open PRs (wrapper for scan_prs.py)
# Usage: bash scripts/scan-prs.sh [N]
set -euo pipefail
exec python "$(dirname "$0")/scan_prs.py" "$@"
