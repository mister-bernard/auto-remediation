#!/usr/bin/env bash
# install.sh — wire the auto-remediation daemon into systemd --user.
#
# Reads TELEGRAM_CHAT_ID from the environment (or prompts) and writes it
# to ~/.config/auto-remediation/env so the systemd unit can find it.
# That file is local-only and never enters the repo.

set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="auto-remediation.service"
ENV_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/auto-remediation"
ENV_FILE="$ENV_DIR/env"

mkdir -p "$UNIT_DIR" "$ENV_DIR"

if [ ! -f "$ENV_FILE" ]; then
  : "${TELEGRAM_CHAT_ID:=}"
  if [ -z "$TELEGRAM_CHAT_ID" ] && [ -t 0 ]; then
    read -rp "Telegram chat ID for escalations: " TELEGRAM_CHAT_ID
  fi
  cat > "$ENV_FILE" <<EOF
# Local-only config for auto-remediation. Not in repo.
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
EOF
  chmod 600 "$ENV_FILE"
  echo "Wrote $ENV_FILE"
fi

cp "$REPO_DIR/systemd/$UNIT_NAME" "$UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

sleep 1
systemctl --user --no-pager status "$UNIT_NAME" | head -15
echo
echo "Tail logs: journalctl --user -u $UNIT_NAME -f"
