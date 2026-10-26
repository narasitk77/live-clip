#!/bin/sh
# One-shot setup on a fresh Ubuntu 22.04/24.04 VM (run as root):
#   curl -fsSL https://raw.githubusercontent.com/narasitk77/live-clip/main/cloud/setup.sh | sh
# Then edit /opt/live-clip/.env and run: cd /opt/live-clip && docker compose -f docker-compose.cloud.yml up -d
set -eu
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh
mkdir -p /opt && cd /opt
[ -d live-clip ] || git clone https://github.com/narasitk77/live-clip.git
cd live-clip && git pull -q
IP=$(curl -fsS https://api.ipify.org)
if [ ! -f .env ]; then
  cp .env.example .env
  # sensible cloud defaults; secrets are generated here and never printed
  gen() { tr -dc 'a-z0-9' </dev/urandom | head -c "$1"; }
  sed -i "s/^SRT_PASSPHRASE=.*/SRT_PASSPHRASE=$(gen 40)/; s/^WEB_PASSWORD=.*/WEB_PASSWORD=$(gen 20)/; s/^MEDIAMTX_API_PASS=.*/MEDIAMTX_API_PASS=$(gen 32)/" .env
  sed -i "s/^SRT_PUBLIC_HOST=.*/SRT_PUBLIC_HOST=$IP/; s/^MIN_FREE_GB=.*/MIN_FREE_GB=10/; s/^CUT_WORKERS=.*/CUT_WORKERS=1/" .env
  printf 'IMAGE_TAG=latest\nPUBLIC_HOST=%s.sslip.io\n' "$(echo "$IP" | tr . -)" >> .env
  echo "wrote /opt/live-clip/.env (secrets inside). Web will be https://$(echo "$IP" | tr . -).sslip.io"
fi
echo "firewall: allow UDP 8890, UDP 8891, TCP 80, TCP 443 at the provider, then:"
echo "  cd /opt/live-clip && docker compose -f docker-compose.cloud.yml up -d"
