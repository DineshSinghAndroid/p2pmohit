#!/bin/bash
set -e
echo "🚀 Pulling latest code from GitHub..."
git pull origin main
echo "📦 Rebuilding and restarting containers..."
sudo docker compose up -d --build
echo "✅ Update complete! App is live."
