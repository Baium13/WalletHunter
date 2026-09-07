#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m compileall core desktop integrations
printf '
Build OK.
'
printf 'Next: cp .env.example .env && nano .env
'
