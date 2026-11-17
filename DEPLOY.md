# Deploy on AWS (Mumbai, ap-south-1)  | budget ~INR 1,000/month

## 1. Server
- AWS Lightsail (simplest) or EC2: Ubuntu 22.04, **2 GB RAM minimum** (Chromium), region **Mumbai**.
- Attach a **static IP** (Lightsail: "Networking > Create static IP"; EC2: Elastic IP).
  This is the IP you allowlist in Cloudflare so the 429s stop.
- Firewall: allow 22 (SSH, only your IP) and 80/443.

## 2. Install Docker
    curl -fsSL https://get.docker.com | sh

## 3. Run the app
    (see setup.sh: one command does all of this)
    ssh ubuntu@<STATIC_IP>
    unzip payout-click-tester.zip && cd payout-click-tester
    docker compose up -d --build
    # open http://<STATIC_IP>:8000

## 4. Fixed link + HTTPS (optional)
Point a DNS record (e.g. tester.yourdomain.com) to the static IP, then put Caddy in front:
    sudo apt install -y caddy
    echo 'tester.yourdomain.com { reverse_proxy localhost:8000 }' | sudo tee /etc/caddy/Caddyfile
    sudo systemctl reload caddy

## 5. Stop the 429s
In Cloudflare: Security > WAF > Tools > IP Access Rules > allow the static IP.

## Notes
- The admin login was removed on purpose (test-only). Anyone with the link can run the tool,
  so restrict port 8000 / the domain to your own IP in the firewall.
- Target email/password are saved on the server in data/saved_settings.json; delete it when done.
- Min amount for test runs: keep at 25,000+.
