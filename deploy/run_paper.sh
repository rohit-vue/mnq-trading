#!/bin/bash
# Non-interactive paper trading (VPS / systemd).
# Same as: menu [2] + Enter on contracts [1]
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/trader/mnq-trading}"
CONTRACTS="${MNQ_CONTRACTS:-1}"

cd "$PROJECT_DIR"
source venv/bin/activate
export PYTHONUNBUFFERED=1
exec python main_v2.py --mode paper --contracts "$CONTRACTS"
