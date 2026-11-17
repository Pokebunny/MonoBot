# Planned: move to a new Graviton server

Decided 2026-11-05 (option B over an in-place resize). Started 2026-11-17,
before turning on the replay upload API: its uploaders send every player's
recording of a game, each parsed separately.

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

## Steps

Who: **you** = AWS console / `ec2-user` (root); **Claude** = scripts, the
MonoBot data copy, secrets, verification (the `monobot` login can't sudo).

1. ~~**Claude** — write `deploy/bootstrap.sh`~~ Done: users, dirs, uv,
   clone, `uv sync`, systemd unit, the sudoers line, a 500 MB journal cap,
   and Caddy for the upload API. No logrotate rule: RioBot rotates its own
   log since malonek1/RioBot#110.
2. **You** — launch the t4g.small (AL2023 arm64, 30 GB gp3, same key pair as
   now), allocate an Elastic IP and attach it. Security group: 22, plus 80
   and 443 for the upload API. Point the upload domain's A record at the
   Elastic IP.
3. **You** — run the bootstrap as `ec2-user` (command in
   `deploy/README.md`, with the domain); set up LTWBot and RioBot too.
   ARM means a fresh install, not a copy: RioBot runs on a hand-built
   Python 3.11 (`/opt/Python-3.11.3`) — rebuild it or move it to uv. Check
   what `codedeploy-agent` on the old box is for (not MonoBot — its deploy is
   SSH from GitHub Actions) and whether the new box needs it.
4. **Claude** — stop MonoBot on the old box, then copy the live-only files:
   `main/.env`, `main/resources/config.json`, `main/resources/monobot.db`,
   `main/resources/guilds/` (every community DB), and the replay archive
   `main/resources/replays/` (~1.7 GB). Copy DBs with sqlite's backup, not a
   raw cp of a live file.
5. **Claude** — point the `DEPLOY_HOST` GitHub secret at the Elastic IP
   (`DEPLOY_SSH_KEY` must have its public half in the new
   `/home/monobot/.ssh/authorized_keys`), push or re-run the deploy, and
   confirm: bot connects, "Serving 3 communities", match counts match, no
   reconcile surprises. Then add `upload_api` to config.json, restart, and
   check `https://<domain>/v1/me` answers 401.
6. **You** — stop (don't terminate) the old instance; terminate after a
   week of the new one running cleanly.

## Watch out for

- Starting MonoBot on both boxes at once means two bots answering every
  command — stop the old one before starting the new one.
- `/tmp` was tmpfs (RAM) on the old box; stage large copies in a home dir.
- Update the local notes and scripts that hard-code the old IP
  (`52.14.192.236`, e.g. `scripts/sync_live_db.py`).
