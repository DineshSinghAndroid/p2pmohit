#!/bin/bash
# One-shot installer for Ubuntu 22.04/24.04 (AWS Lightsail). Run as the default user (ubuntu).
set -e
ZIP_URL="$1"
[ -z "$ZIP_URL" ] && { echo "usage: setup.sh <zip-url>"; exit 1; }
sudo apt-get update -y && sudo apt-get install -y unzip curl
command -v docker >/dev/null || { curl -fsSL https://get.docker.com | sudo sh; }
sudo usermod -aG docker "$USER" || true
# small swap so Chromium never runs out of memory on a 2 GB box
if ! swapon --show | grep -q swap; then sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile; fi
cd ~ && rm -rf payout-click-tester && mkdir payout-click-tester && cd payout-click-tester
curl -fsSL "$ZIP_URL" -o app.zip && unzip -q app.zip && cd pct
sudo docker compose up -d --build
IP=$(curl -s https://checkip.amazonaws.com)
echo; echo "DONE. Open: http://$IP:8000"
