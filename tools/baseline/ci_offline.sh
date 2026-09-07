#!/usr/bin/env bash
# Linux-only CI boundary. Provision dependencies BEFORE this script.
# There is deliberately no fallback if network-namespace isolation fails.
set -euo pipefail
phase0_python="$(pwd)/.phase0/venv/bin/python"
phase0_node="$(command -v node)"
phase0_uid="$(id -u)"
phase0_gid="$(id -g)"
phase0_temp="$(mktemp -d)"
phase0_path="$(dirname "$phase0_python"):$(dirname "$phase0_node"):/usr/bin:/bin"
sudo --non-interactive unshare --net -- setpriv \
  --reuid "$phase0_uid" --regid "$phase0_gid" --clear-groups \
  --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs \
  env -i PATH="$phase0_path" HOME="$phase0_temp" TMPDIR="$phase0_temp" \
  LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  BASELINE_OS_NETWORK_SANDBOX=linux-network-namespace \
  "$phase0_python" -B tools/baseline/ci_checks.py \
  --python "$phase0_python" --node "$phase0_node"
