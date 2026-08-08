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

Amazon Linux is not Ubuntu, and the difference bites in two places: `adduser`
here is a symlink to shadow-utils' `useradd`, whose options are entirely
different from Debian's, and `git` is not on the base AMI. The admin user is
`ec2-user`, not `ubuntu`.

```bash
# as ec2-user
sudo dnf install -y git          # yum on Amazon Linux 2

# --create-home is required: --system otherwise skips the home directory, and
# no home means no ~/.ssh/authorized_keys for the deploy key to live in.
# --shell is required too: the default for a system account is nologin, and a
# nologin user cannot open an SSH session at all.
sudo useradd --system --user-group --create-home --home-dir /home/monobot \
    --shell /bin/bash monobot
sudo mkdir -p /opt/monobot && sudo chown monobot:monobot /opt/monobot

# as monobot (sudo -u monobot -s)
curl -LsSf https://astral.sh/uv/install.sh | sh          # installs to ~/.local/bin
cd /opt/monobot && git clone https://github.com/Pokebunny/MonoBot.git
cd MonoBot && ~/.local/bin/uv sync

# live-only files (gitignored) — copy from your machine:
#   main/.env               BOT_TOKEN=...
#   main/resources/config.json
#   main/resources/monobot.db   (and pubs.db if you want the pubs commands)
# e.g. scp -r main/.env main/resources/*.json main/resources/*.db ec2-user@HOST:/tmp/
#      then sudo mv them into place and chown monobot:monobot

# service
sudo cp deploy/monobot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now monobot
journalctl -u monobot -f        # watch it come up
```

Let the deploy script restart the service without a password. This grants
`monobot` exactly one command, and only for its own unit:

```bash
echo 'monobot ALL=(root) NOPASSWD: /usr/bin/systemctl restart monobot' | sudo tee /etc/sudoers.d/monobot
sudo chmod 440 /etc/sudoers.d/monobot && sudo visudo -cf /etc/sudoers.d/monobot
```

LTWBot has this whole sequence as a single idempotent script
([`deploy/bootstrap.sh`](https://github.com/Pokebunny/LTWBot/blob/main/deploy/bootstrap.sh),
which handles both distro families). Worth porting here the next time this
needs doing from scratch.

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
