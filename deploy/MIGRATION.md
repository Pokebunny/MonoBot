# Move to a new Graviton server

Decided 2026-11-05 (option B over an in-place resize). **Cut over
2026-11-17**: all three bots run on the t4g.small at Elastic IP
`18.224.76.73` (`i-...`, private `ip-172-31-0-200`); the upload API is at
https://monobot-uploads.duckdns.org (DuckDNS). The old t2.micro
(`i-01de6825d150fc7d7`, `52.14.192.236`) has its bots stopped and disabled;
step 8 remains.

## Why

The current box is a **t2.micro** (1 vCPU, 1 GB RAM, 8 GB disk, Amazon Linux
2023, us-east-2c) shared by MonoBot, LTWBot and RioBot. At 6,500 games on the
Monobattle server:

- **Disk** is the first wall: ~240 MB per 1,000 replays, 1.5 GB free → full
  around ~12k games. RioBot's `/var/log/riobot.log` filled it twice before
  RioBot started rotating it (2026-11-07).
- **Memory**: bot ~280–320 MB, ~340 MB available → swapping around ~13–14k
  games.
- **CPU**: t2 is credit-throttled to ~10% of a vCPU once credits run out;
  rebuilds after an upload take ~5 s and grow ~0.8 s per 1,000 games. Check
  EC2 → Monitoring → "CPU credit balance" for past throttling.

## Target

- **t4g.small** (2 vCPU, 2 GB, ~$12/mo; chosen 2026-11-17) — Graviton (ARM),
  Amazon Linux 2023. Unlimited credit mode (the t4g default): bursts are
  never throttled, sustained load above baseline bills a little extra.
  Resizing to t4g.medium (4 GB) later is stop, change type, start.
- **30 GB gp3** root volume.
- An **Elastic IP**, so the address survives stops and later moves.
- Expected: rebuilds ~1.5–2x faster, no credit throttling, memory headroom to
  ~50k+ games.

## The three bots

| | MonoBot | LTWBot | RioBot |
|---|---|---|---|
| Deploy | GitHub Actions → SSH → `deploy/update.sh` | same, its own repo | GitHub Actions → **AWS CodeDeploy** (`RioBot` / `RioBot_deployment_group`) |
| Runs as | `monobot`, systemd | `ltwbot`, systemd | root, `nohup python3.11` (CodeDeploy hooks) |
| Lives in | `/opt/monobot/MonoBot` | `/opt/ltwbot/LTWBot` | `/home/ec2-user/RioBot` |
| Live-only files | `main/.env`, `resources/config.json`, `monobot.db`, `guilds/`, `replays/` | `.env`, `client_secret.json`, `trivia_stats.txt` | `main/.env`, `main/rio_name_map.json` |
| Repoint after | `DEPLOY_HOST` secret | `DEPLOY_HOST` secret (LTWBot repo) | CodeDeploy group's target |

ARM is a fresh install, not a copy: everything is reinstalled from the
repos, and only the live-only files move. RioBot's install hook takes Python
3.11 from dnf, so it needs nothing hand-built.

## Steps

Who: **you** = AWS console / `ec2-user`; **Claude** = scripts, GitHub
secrets, verification. Steps 1–4 are prep with every bot still running on
the old box; the downtime is steps 5–7 (~15 minutes).

1. ~~**Claude** — the scripts~~ Done: `deploy/bootstrap.sh` (MonoBot,
   + Caddy), `deploy/new_box.sh` (all three bots), `deploy/pull_live_files.sh`
   (cutover copy). No logrotate rule: RioBot rotates its own log since
   malonek1/RioBot#110.
2. **You** — in the console:
   - `RioBot_deployment_group` (us-east-2) targets any EC2 instance with the
     tag key **`bot_host`** (key only; the value isn't checked). Note which
     **IAM role** the old box has (EC2 → instance → Security): the
     CodeDeploy agent needs that role to work.
   - Launch the t4g.small: AL2023 **arm64**, 30 GB gp3, same key pair, the
     **same IAM role**, and **no `bot_host` tag** yet (with it, the next
     RioBot deploy would start a second RioBot here).
   - Allocate an Elastic IP and attach it. Security group: 22, 80, 443.
   - Point the upload domain's A record at the Elastic IP.
3. **You** — on the new box as `ec2-user`, with each repo's deploy public
   key (old box: `/home/monobot/.ssh/authorized_keys`,
   `/home/ltwbot/.ssh/authorized_keys`):
   ```
   curl -fsSL https://raw.githubusercontent.com/Pokebunny/MonoBot/main/deploy/new_box.sh \
     | bash -s -- "<monobot pubkey>" "<ltwbot pubkey>" <upload domain>
   ```
4. **You** — let the new box reach the old one: on the new box
   `ssh-keygen -t ed25519 -N "" -f ~/.ssh/old_box`, and append
   `~/.ssh/old_box.pub` to the old box's `/home/ec2-user/.ssh/authorized_keys`.
5. **You** — cutover. On the new box:
   ```
   curl -fsSLO https://raw.githubusercontent.com/Pokebunny/MonoBot/main/deploy/pull_live_files.sh
   bash pull_live_files.sh <old box IP>
   ```
   It stops and disables all three bots on the old box, copies the files,
   fixes ownership, and checks every database's match count against the
   old box's.
6. **You** — `sudo systemctl start monobot ltwbot`. For RioBot: move the
   `bot_host` tag from the old instance to the new one, then CodeDeploy →
   the group's last deployment → **Retry** (or re-run RioBot's latest
   "CD Pipeline" action). Its hooks install the deps and start the bot.
7. **Claude** — set `DEPLOY_HOST` to the Elastic IP in the MonoBot and
   LTWBot repos, re-run each deploy, and confirm: all three bots online,
   "Serving 3 communities", no reconcile surprises. Then add `upload_api`
   to config.json, restart, and check `https://<domain>/v1/me` answers 401.
8. **You** — stop (don't terminate) the old instance; terminate it after a
   week of the new one running cleanly.

## Watch out for

- Any bot running on both boxes at once answers every command twice.
  `pull_live_files.sh` stops and disables the old copies before anything
  starts on the new box; RioBot also needs its CodeDeploy tag moved, not
  copied.
- Rollback until the old box is terminated: on it,
  `sudo systemctl enable --now monobot ltwbot`, move the RioBot tag back and
  redeploy, and restore the `DEPLOY_HOST` secrets.
- `/tmp` was tmpfs (RAM) on the old box; stage large copies in a home dir.
- Update the local notes and scripts that hard-code the old IP
  (`52.14.192.236`: `scripts/sync_live_db.py`, the "MonoBot live DB sync"
  scheduled task's target, `~/.ssh` known_hosts).
