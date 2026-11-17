# Deploying MonoBot to AWS

One small always-on Linux box is the right shape for this bot (single process,
SQLite on local disk). It runs on an **EC2 instance running Amazon Linux**,
which it shares with LTWBot - separate unix users, directories and systemd
units, nothing shared but the kernel:

```
/opt/monobot/MonoBot  ->  monobot.service  (user monobot)
/opt/ltwbot/LTWBot    ->  ltwbot.service   (user ltwbot)
```

Access is via the AWS browser console (EC2 Instance Connect) unless you've put
a personal key in `ec2-user`'s `authorized_keys`. The `monobot` deploy key is
for GitHub Actions - avoid using it as a general login, or rotating it later
also locks you out.

## One-time server setup

`deploy/bootstrap.sh` does the whole thing on Amazon Linux 2023 (x86 or
Graviton) and is safe to re-run. As `ec2-user`:

```bash
curl -fsSL https://raw.githubusercontent.com/Pokebunny/MonoBot/main/deploy/bootstrap.sh \
  | bash -s -- "<deploy key's public half>" uploads.example.com
```

It creates the `monobot` user, installs uv and the repo, authorises the
GitHub Actions deploy key, installs the systemd unit and the one sudoers rule
`update.sh` needs (restarting this unit, nothing else), and caps the journal
at 500 MB. With the optional domain it also sets up Caddy for the upload API
(next section). The service is left enabled but stopped until you copy in the
live-only files, which aren't in the repo: `main/.env` (`BOT_TOKEN=...`),
`main/resources/config.json`, `main/resources/monobot.db`,
`main/resources/guilds/` and the replay archive `main/resources/replays/`.

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

LTWBot deploys to the same host from its own repo, with its own key and its own
`DEPLOY_*` secrets. The two are independent: revoking one bot's key doesn't
affect the other.

Security group: allow inbound SSH (22), plus 80 and 443 if the replay upload
API is on (next section). Otherwise the bot only makes outbound connections.

## Replay upload API

Players' desktop uploaders (`uploader/`) POST replays to the bot over HTTPS.
The bot serves plain HTTP on 127.0.0.1 (`upload_api.port` in config.json), and
Caddy sits in front to handle TLS, getting and renewing a Let's Encrypt
certificate by itself. Let's Encrypt needs a DNS name, not a bare IP: point an
A record (any domain you own, or a free one such as DuckDNS) at the Elastic IP.

`bootstrap.sh` installs and configures Caddy when given the domain (it
proxies to port 8080; keep `upload_api.port` in step). Point the A record and
open 80 (the certificate challenge) and 443 in the security group first, or
Caddy can't get its certificate. Then set `upload_api` in config.json (see
`uploader/README.md`) and restart the bot.
`curl https://uploads.example.com/v1/me` should answer 401 with an "unknown
token" message.

## Database backups

The DBs hold user-written data (links, merges, confirmed winners) that no
replay can regenerate. Nightly copy to S3:

```bash
# /etc/cron.d/monobot-backup  (bucket must exist; instance role or aws configure)
0 9 * * * monobot sqlite3 /opt/monobot/MonoBot/main/resources/monobot.db ".backup /tmp/monobot-backup.db" && aws s3 cp /tmp/monobot-backup.db s3://YOUR-BUCKET/monobot/monobot-$(date +\%F).db
```

`sqlite3 .backup` is safe against a live writer (plain `cp` is not).

Each extra community (a server under `guilds` in config.json) has its own
database at `main/resources/guilds/<guild_id>/monobot.db`, created on first
start — back those up the same way.
