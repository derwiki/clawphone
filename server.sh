#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"

trap 'exit 0' INT TERM

while true; do
  git pull --ff-only origin main || echo "git pull failed, continuing with current code"
  uv run python main.py
  echo "server exited with status $?, restarting in 1s..."
  sleep 1
done
