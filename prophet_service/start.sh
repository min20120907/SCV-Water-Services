#!/usr/bin/env bash
# Start the Prophet prediction service
# Usage: ./start.sh [port]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:-8765}"

cd "$SCRIPT_DIR"
exec python3 main.py
