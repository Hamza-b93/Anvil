#!/usr/bin/env bash
# Anvil dev launcher — sets up a venv on first run, then starts the server.
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment…"
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -e .

echo ""
echo "Anvil is starting at http://127.0.0.1:8000"
echo "Privileged actions (sync/apply/remove) will prompt via your desktop's polkit dialog."
echo "Press Ctrl+C to stop."
echo ""

exec anvil
