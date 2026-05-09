#!/usr/bin/env bash
# setup.sh — One-shot deployment script for youtube-transcripts on a Debian/Ubuntu Linode.
#
# Run as root:   bash setup.sh
#
# What this does, step by step:
#   1. Installs system dependencies (python3-venv, nginx, ffmpeg)
#   2. Installs Tailscale (if not already present)
#   3. Creates a dedicated low-privilege user to run the service
#   4. Copies the app files to /opt/youtube-transcripts
#   5. Creates a Python virtualenv and installs pip packages
#   6. Sets up the systemd service
#   7. Configures nginx to listen on the Tailscale interface only
#   8. Enables and starts everything

set -euo pipefail

APP_DIR="/opt/youtube-transcripts"
SERVICE_USER="ytscripts"
LOG_DIR="/var/log/youtube-transcripts"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colour helpers ─────────────────────────────────────────────────────────────
green()  { echo -e "\033[0;32m[OK]\033[0m $*"; }
yellow() { echo -e "\033[0;33m[..]\033[0m $*"; }
red()    { echo -e "\033[0;31m[!!]\033[0m $*"; }

# ── 0. Must be root ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  red "Run this script as root:  sudo bash setup.sh"
  exit 1
fi

# ── 1. System packages ─────────────────────────────────────────────────────────
yellow "Updating package list and installing dependencies..."
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip nginx ffmpeg curl

green "System packages installed."

# ── 2. Tailscale ───────────────────────────────────────────────────────────────
if ! command -v tailscale &>/dev/null; then
  yellow "Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
  green "Tailscale installed. Run 'tailscale up' to authenticate and join your mesh."
  echo ""
  echo "  ┌─────────────────────────────────────────────────────┐"
  echo "  │  ACTION REQUIRED:                                   │"
  echo "  │  Run:  tailscale up                                 │"
  echo "  │  Then note your Tailscale IP:  tailscale ip -4      │"
  echo "  │  You'll need it to update the nginx config.         │"
  echo "  └─────────────────────────────────────────────────────┘"
  echo ""
else
  green "Tailscale already installed."
fi

# ── 3. Dedicated service user ──────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
  yellow "Creating service user '$SERVICE_USER'..."
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  green "User '$SERVICE_USER' created."
else
  green "User '$SERVICE_USER' already exists."
fi

# ── 4. Copy app files ──────────────────────────────────────────────────────────
yellow "Copying app files to $APP_DIR..."
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/app.py"          "$APP_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"
cp -r "$SCRIPT_DIR/templates"    "$APP_DIR/"

mkdir -p "$APP_DIR/transcripts"
mkdir -p "$LOG_DIR"

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

green "App files copied."

# ── 5. Python virtualenv + pip installs ────────────────────────────────────────
yellow "Creating Python virtualenv and installing packages..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
green "Python dependencies installed."

# ── 6. systemd service ─────────────────────────────────────────────────────────
yellow "Installing systemd service..."
cp "$SCRIPT_DIR/youtube-transcripts.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable youtube-transcripts
systemctl restart youtube-transcripts
green "Service enabled and started."

# ── 7. nginx config ────────────────────────────────────────────────────────────
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || true)"

if [[ -z "$TAILSCALE_IP" ]]; then
  red "Could not detect Tailscale IP — Tailscale may not be authenticated yet."
  yellow "After running 'tailscale up', manually edit /etc/nginx/sites-available/youtube-transcripts"
  yellow "and replace 100.x.x.x with your actual Tailscale IP, then run: nginx -t && systemctl reload nginx"
  cp "$SCRIPT_DIR/nginx-youtube-transcripts.conf" /etc/nginx/sites-available/youtube-transcripts
else
  yellow "Configuring nginx for Tailscale IP $TAILSCALE_IP..."
  sed "s/100\.x\.x\.x/$TAILSCALE_IP/g" \
    "$SCRIPT_DIR/nginx-youtube-transcripts.conf" \
    > /etc/nginx/sites-available/youtube-transcripts
  green "nginx config written with IP $TAILSCALE_IP."
fi

# Enable the site (remove default if it's conflicting)
ln -sf /etc/nginx/sites-available/youtube-transcripts /etc/nginx/sites-enabled/youtube-transcripts

# Test nginx config before reloading — never blindly reload
if nginx -t 2>/dev/null; then
  systemctl reload nginx
  green "nginx reloaded."
else
  red "nginx config test failed. Check /etc/nginx/sites-available/youtube-transcripts"
  red "Run 'nginx -t' for details."
fi

# ── 8. Done ────────────────────────────────────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────────────────────┐"
echo "  │  Setup complete!                                        │"
if [[ -n "$TAILSCALE_IP" ]]; then
echo "  │  Open in your browser:  http://$TAILSCALE_IP           │"
fi
echo "  │                                                         │"
echo "  │  Useful commands:                                       │"
echo "  │    systemctl status youtube-transcripts                 │"
echo "  │    journalctl -u youtube-transcripts -f                 │"
echo "  │    tail -f /var/log/youtube-transcripts/access.log      │"
echo "  └─────────────────────────────────────────────────────────┘"
echo ""
