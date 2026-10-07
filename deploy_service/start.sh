#!/usr/bin/env bash
# Start the Deploy service
# Usage: ./start.sh [port]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8767}"

cd "$SCRIPT_DIR"
exec python3 main.py
