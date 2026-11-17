#!/usr/bin/env bash
# Set up a fresh Amazon Linux 2023 box for all three bots that share it:
# MonoBot and LTWBot (each repo's own bootstrap.sh) and RioBot (deployed by
# AWS CodeDeploy, so the box needs Python 3.11 and the CodeDeploy agent).
# Run as ec2-user. Safe to re-run. See deploy/MIGRATION.md for the order.
#
#   curl -fsSL https://raw.githubusercontent.com/Pokebunny/MonoBot/main/deploy/new_box.sh \
#     | bash -s -- "<monobot deploy pubkey>" "<ltwbot deploy pubkey>" [upload domain]
#
# The pubkeys are the public halves of each repo's DEPLOY_SSH_KEY secret (on
# the old box: /home/monobot/.ssh/authorized_keys, /home/ltwbot/.ssh/authorized_keys).
set -euo pipefail

MONOBOT_PUBKEY="${1:-}"
LTWBOT_PUBKEY="${2:-}"
UPLOAD_DOMAIN="${3:-}"
if [ -z "$MONOBOT_PUBKEY" ] || [ -z "$LTWBOT_PUBKEY" ]; then
    echo "usage: new_box.sh \"<monobot pubkey>\" \"<ltwbot pubkey>\" [upload domain]" >&2
    exit 1
fi

echo "=== MonoBot"
curl -fsSL https://raw.githubusercontent.com/Pokebunny/MonoBot/main/deploy/bootstrap.sh \
    | bash -s -- "$MONOBOT_PUBKEY" "$UPLOAD_DOMAIN"

echo "=== LTWBot"
curl -fsSL https://raw.githubusercontent.com/Pokebunny/LTWBot/main/deploy/bootstrap.sh \
    | bash -s -- "$LTWBOT_PUBKEY"

echo "=== RioBot (CodeDeploy)"
# Its install hook runs as root with /usr/bin/python3.11 (malonek1/RioBot
# scripts/install_dependencies.sh); the agent is Ruby.
sudo dnf install -y python3.11 python3.11-pip ruby wget
if ! systemctl is-active --quiet codedeploy-agent; then
    # IMDSv2: the region this box runs in, for the agent's regional bucket.
    imds_token=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
    region=$(curl -fsS -H "X-aws-ec2-metadata-token: $imds_token" \
        http://169.254.169.254/latest/meta-data/placement/region)
    wget -q "https://aws-codedeploy-$region.s3.$region.amazonaws.com/latest/install" -O /tmp/codedeploy-install
    chmod +x /tmp/codedeploy-install
    sudo /tmp/codedeploy-install auto
    rm /tmp/codedeploy-install
fi
sudo systemctl enable --now codedeploy-agent
sudo mkdir -p /home/ec2-user/RioBot/main
sudo chown -R ec2-user:ec2-user /home/ec2-user/RioBot

cat <<'EOF'

Box ready. All three bots are installed but stopped. Next (deploy/MIGRATION.md):
  1. give this box ssh access to the old one, then run deploy/pull_live_files.sh
  2. start monobot and ltwbot; move RioBot's CodeDeploy target here and redeploy
EOF
