#!/usr/bin/env bash
# Cutover: stop all three bots on the OLD box and copy every live-only file
# (secrets, databases, replay archive, bot-written state) to this new one.
# Run on the new box as ec2-user, after deploy/new_box.sh, once this box can
# ssh to the old one as ec2-user (deploy/MIGRATION.md).
#
#   bash pull_live_files.sh <old box address> [ssh key, default ~/.ssh/old_box]
#
# The bots are down from here until started on the new box. Rollback: on the
# old box, `sudo systemctl enable --now monobot ltwbot` and redeploy RioBot there.
set -euo pipefail

OLD="${1:-}"
KEY="${2:-$HOME/.ssh/old_box}"
if [ -z "$OLD" ]; then
    echo "usage: pull_live_files.sh <old box address> [ssh key]" >&2
    exit 1
fi
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "ec2-user@$OLD")

MONO=/opt/monobot/MonoBot/main
LTW=/opt/ltwbot/LTWBot
RIO=/home/ec2-user/RioBot/main
REQUIRED=(
    "$MONO/.env" "$MONO/resources/config.json" "$MONO/resources/monobot.db"
    "$MONO/resources/guilds" "$MONO/resources/replays"
    "$LTW/.env" "$LTW/client_secret.json"
    "$RIO/.env"
)
# Written by the bots as they run, so absent until first written.
OPTIONAL=("$MONO/resources/monobot.db-journal" "$LTW/trivia_stats.txt" "$RIO/rio_name_map.json")

echo "=== Checking the old box"
"${SSH[@]}" "for p in ${REQUIRED[*]}; do sudo test -e \"\$p\" || { echo \"missing on old box: \$p\" >&2; exit 1; }; done"
present=$("${SSH[@]}" "for p in ${OPTIONAL[*]}; do sudo test -e \"\$p\" && echo \"\$p\"; done; true" | tr '\n' ' ')

echo "=== Stopping the bots on the old box"
# disable too, so a reboot of the old box can't bring a second copy online.
# [R]ioBot: a plain pattern would match this remote shell's own command line.
"${SSH[@]}" 'sudo systemctl disable --now monobot ltwbot
sudo systemctl disable --now riobot 2>/dev/null || true
sudo pkill -f "[R]ioBot\.py" || true
sleep 2
if pgrep -f "[R]ioBot\.py|[M]onoBot\.py|[L]TWBot\.py" >/dev/null; then echo "a bot is still running" >&2; exit 1; fi'

echo "=== Copying (the replay archive is ~2 GB; a few minutes)"
# Bots are stopped, so the SQLite files are at rest and safe to copy raw.
# Backups (*.bak*) stay behind on the old box. Paths relative to /.
relative=()
for p in "${REQUIRED[@]}" $present; do relative+=("${p#/}"); done
"${SSH[@]}" "sudo tar -C / --exclude='*.bak*' -cf - ${relative[*]}" | sudo tar -C / -xpf -

echo "=== Fixing ownership"
sudo chown -R monobot:monobot "$MONO"
sudo chown -R ltwbot:ltwbot "$LTW"
sudo chown -R ec2-user:ec2-user /home/ec2-user/RioBot
sudo chmod 600 "$MONO/.env" "$LTW/.env" "$LTW/client_secret.json" "$RIO/.env"

echo "=== Match counts, old vs new"
for db in "$MONO/resources/monobot.db" "$MONO"/resources/guilds/*/monobot.db; do
    new=$(sudo sqlite3 "file:$db?mode=ro" "SELECT COUNT(*) FROM matches")
    old=$("${SSH[@]}" "sudo python3 -c \"import sqlite3; print(sqlite3.connect('file:$db?mode=ro', uri=True).execute('SELECT COUNT(*) FROM matches').fetchone()[0])\"")
    echo "$db: old $old, new $new"
    [ "$old" = "$new" ] || { echo "MISMATCH" >&2; exit 1; }
done

cat <<EOF

Copied. Old box bots are stopped and disabled. Next (deploy/MIGRATION.md):
    sudo systemctl start monobot ltwbot
then move RioBot's CodeDeploy target to this box and redeploy master.
EOF
