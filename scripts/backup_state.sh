#!/usr/bin/env bash
set -euo pipefail
root=/home/ubuntu/WalletHunterV07/WalletHunterV05_final
exec "$root/.venv/bin/python" "$root/scripts/backup_runtime.py"
