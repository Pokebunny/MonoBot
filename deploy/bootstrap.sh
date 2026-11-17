#!/usr/bin/env bash
# One-time server setup for MonoBot on Amazon Linux 2023 (x86 or Graviton).
# Run on the box as ec2-user. Safe to re-run: every step is idempotent.
#
#   curl -fsSL https://raw.githubusercontent.com/Pokebunny/MonoBot/main/deploy/bootstrap.sh \
#     | bash -s -- "ssh-ed25519 AAAA... monobot github actions deploy key" [uploads.example.com]
#
# The first argument is the *public* half of the GitHub Actions deploy key
# (private half = the DEPLOY_SSH_KEY repo secret). The optional second one is
# the upload API's domain: with it, Caddy is installed to serve HTTPS there
# (its A record must already point at this box, and ports 80/443 be open).
#
# Afterwards, copy the live-only files in (see deploy/MIGRATION.md) and start
# the service.
set -euo pipefail

DEPLOY_PUBKEY="${1:-}"
UPLOAD_DOMAIN="${2:-}"
if [ -z "$DEPLOY_PUBKEY" ]; then
    echo "usage: bootstrap.sh \"<ssh public key>\" [upload domain]" >&2
    exit 1
fi

REPO=https://github.com/Pokebunny/MonoBot.git
HOME_DIR=/home/monobot
APP_DIR=/opt/monobot/MonoBot
# Must match upload_api.port in config.json.
UPLOAD_API_PORT=8080

# git isn't on the base AMI; sqlite3 is for backups (deploy/README.md).
sudo dnf install -y git sqlite

# --system skips the home directory without --create-home, and defaults to a
# nologin shell, which can't open the deploy key's SSH session.
if ! id monobot >/dev/null 2>&1; then
    sudo useradd --system --user-group --create-home --home-dir "$HOME_DIR" --shell /bin/bash monobot
    echo "created user monobot"
else
    echo "user monobot already exists"
fi
sudo mkdir -p /opt/monobot
sudo chown monobot:monobot /opt/monobot

# Authorise the GitHub Actions deploy key.
sudo -u monobot mkdir -p "$HOME_DIR/.ssh"
sudo -u monobot chmod 700 "$HOME_DIR/.ssh"
sudo -u monobot touch "$HOME_DIR/.ssh/authorized_keys"
if ! sudo grep -qF "$DEPLOY_PUBKEY" "$HOME_DIR/.ssh/authorized_keys"; then
    echo "$DEPLOY_PUBKEY" | sudo -u monobot tee -a "$HOME_DIR/.ssh/authorized_keys" >/dev/null
    echo "authorised the deploy key"
else
    echo "deploy key already authorised"
fi
sudo -u monobot chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# uv, the repo, and its dependencies - all as monobot.
sudo -u monobot -H bash -lc '[ -x ~/.local/bin/uv ] || curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u monobot -H bash -lc "[ -d $APP_DIR/.git ] || git clone $REPO $APP_DIR"
sudo -u monobot -H bash -lc "cd $APP_DIR && git fetch origin main && git reset --hard origin/main && ~/.local/bin/uv sync"

# Let deploy/update.sh restart the service without a password: one command,
# only this unit. sudo matches the resolved path, so it must be the one
# update.sh calls.
if [ ! -x /usr/bin/systemctl ]; then
    echo "expected systemctl at /usr/bin/systemctl; update this script, update.sh and the sudoers rule together" >&2
    exit 1
fi
echo 'monobot ALL=(root) NOPASSWD: /usr/bin/systemctl restart monobot' | sudo tee /etc/sudoers.d/monobot >/dev/null
sudo chmod 440 /etc/sudoers.d/monobot
sudo visudo -cf /etc/sudoers.d/monobot

sudo cp "$APP_DIR/deploy/monobot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable monobot >/dev/null

# The journal helped fill the old box's 8 GB disk; cap it.
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' | sudo tee /etc/systemd/journald.conf.d/size.conf >/dev/null
sudo systemctl restart systemd-journald

# Caddy: HTTPS for the upload API, certificates from Let's Encrypt. AL2023 has
# no package, so take the official static build for this CPU.
if [ -n "$UPLOAD_DOMAIN" ]; then
    case "$(uname -m)" in
        aarch64) CADDY_ARCH=arm64 ;;
        x86_64) CADDY_ARCH=amd64 ;;
        *) echo "no Caddy build for $(uname -m)" >&2; exit 1 ;;
    esac
    if ! command -v caddy >/dev/null 2>&1; then
        curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=$CADDY_ARCH" -o /tmp/caddy
        sudo install -m 755 /tmp/caddy /usr/local/bin/caddy
        rm /tmp/caddy
    fi
    if ! id caddy >/dev/null 2>&1; then
        sudo useradd --system --user-group --create-home --home-dir /var/lib/caddy --shell /sbin/nologin caddy
    fi
    sudo mkdir -p /etc/caddy
    printf '%s {\n    reverse_proxy 127.0.0.1:%s\n}\n' "$UPLOAD_DOMAIN" "$UPLOAD_API_PORT" \
        | sudo tee /etc/caddy/Caddyfile >/dev/null
    sudo tee /etc/systemd/system/caddy.service >/dev/null <<'EOF'
[Unit]
Description=Caddy (HTTPS for MonoBot's upload API)
After=network-online.target
Wants=network-online.target

[Service]
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable caddy >/dev/null
    sudo systemctl restart caddy
    echo "caddy serving https://$UPLOAD_DOMAIN -> 127.0.0.1:$UPLOAD_API_PORT"
fi

cat <<EOF

Server setup done. MonoBot is enabled but NOT started: it needs the live-only
files, which aren't in the repo. Copy them in (deploy/MIGRATION.md, step 4):

    $APP_DIR/main/.env
    $APP_DIR/main/resources/config.json
    $APP_DIR/main/resources/monobot.db
    $APP_DIR/main/resources/guilds/
    $APP_DIR/main/resources/replays/

    sudo chown -R monobot:monobot $APP_DIR/main
    sudo systemctl start monobot
    journalctl -u monobot -f
EOF
