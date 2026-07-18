# Deploying MonoBot to AWS

One small always-on Linux box is the right shape for this bot (single process,
SQLite on local disk). Cheapest good options: **EC2 t4g.nano** (~$3/mo, ARM) or
**Lightsail** ($5/mo, simpler console). Ubuntu 24.04 either way.

## One-time server setup

```bash
# as the default ubuntu user
sudo adduser --system --group --home /home/monobot monobot
sudo mkdir -p /opt/monobot && sudo chown monobot:monobot /opt/monobot

# as monobot (sudo -u monobot -s)
curl -LsSf https://astral.sh/uv/install.sh | sh          # installs to ~/.local/bin
cd /opt/monobot && git clone https://github.com/Pokebunny/MonoBot.git
cd MonoBot && ~/.local/bin/uv sync

# live-only files (gitignored) — copy from your machine:
#   main/.env               BOT_TOKEN=...
#   main/resources/config.json
#   main/resources/monobot.db   (and pubs.db if you want the pubs commands)
# e.g. scp -r main/.env main/resources/*.json main/resources/*.db ubuntu@HOST:...

# service
sudo cp deploy/monobot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now monobot
journalctl -u monobot -f        # watch it come up
```

Let the deploy script restart the service without a password:

```bash
echo 'monobot ALL=(root) NOPASSWD: /usr/bin/systemctl restart monobot' | sudo tee /etc/sudoers.d/monobot
```

## Auto-deploy on merge to main

`.github/workflows/deploy.yml` runs tests on every push/PR; on a push to main
it then SSHes to the server and runs `deploy/update.sh` (pull + `uv sync` +
restart). Set three **repository secrets** (GitHub → Settings → Secrets and
variables → Actions):

- `DEPLOY_HOST` — the server's public IP or DNS name
- `DEPLOY_USER` — `monobot`
- `DEPLOY_SSH_KEY` — a private key whose public half is in
  `/home/monobot/.ssh/authorized_keys` (generate a dedicated pair:
  `ssh-keygen -t ed25519 -f deploy_key -N ""`; never reuse your personal key)

Security group: allow inbound SSH (22) only; the bot makes outbound
connections to Discord and needs nothing else open.

## Database backups

The DBs hold user-written data (links, merges, confirmed winners) that no
replay can regenerate. Nightly copy to S3:

```bash
# /etc/cron.d/monobot-backup  (bucket must exist; instance role or aws configure)
0 9 * * * monobot sqlite3 /opt/monobot/MonoBot/main/resources/monobot.db ".backup /tmp/monobot-backup.db" && aws s3 cp /tmp/monobot-backup.db s3://YOUR-BUCKET/monobot/monobot-$(date +\%F).db
```

`sqlite3 .backup` is safe against a live writer (plain `cp` is not).
