#!/usr/bin/env bash
# Run on the server by the GitHub Actions deploy job (and safe to run by hand).
# Pulls main, syncs deps, restarts the bot. Databases, .env, and config.json
# live on the server and are gitignored, so a pull never touches them.
set -euo pipefail

cd "$(dirname "$0")/.."
git fetch origin main
git reset --hard origin/main
~/.local/bin/uv sync
sudo /usr/bin/systemctl restart monobot
echo "Deployed $(git rev-parse --short HEAD)"
