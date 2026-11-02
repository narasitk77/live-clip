#!/bin/sh
# Fresh VM (Ubuntu 24.04 or Debian 13, run as root) -> live-clip taking RTMP + SRT with per-stage
# passwords (docker-compose.rtmp.yml).
#   curl -fsSL https://raw.githubusercontent.com/narasitk77/live-clip/main/cloud/rtmp-setup.sh | sh -s <hostname>
# <hostname> = the DNS name IT points at this VM (DNS only / grey cloud), e.g. liveclip.thestandard.co.
# Re-running keeps every password, but never re-run while a stage is live: it git-pulls the repo and
# may recreate MediaMTX (both stages drop).
# Starts MediaMTX + web only. Caddy (HTTPS) must wait until that name resolves to this VM, because
# Let's Encrypt allows only 5 failed validations per hour: start it later with
#   cd /opt/live-clip && docker compose up -d
# Secrets land in /opt/live-clip/.env and /root/live-clip-senders.txt (both 0600); none are printed.

# Everything sits in main() so a download cut short by "curl | sh" runs nothing at all.
main() {
set -eu
HOST=${1:?usage: rtmp-setup.sh <hostname, e.g. liveclip.thestandard.co>}
# Lowercase name with a dot that ends in letters: an IP or a typo here breaks HTTPS later.
case $HOST in
  *[!a-z0-9.-]* | [!a-z0-9]* | *[!a-z] | *..*) echo "not a hostname: $HOST" >&2; exit 1 ;;
  *.*) ;;
  *) echo "not a hostname: $HOST" >&2; exit 1 ;;
esac

# A fresh VM often has unattended-upgrades holding the dpkg lock: make every apt-get (ours and
# get.docker.com's) wait for it instead of failing at once.
echo 'DPkg::Lock::Timeout "600";' >/etc/apt/apt.conf.d/90live-clip-lock-wait
command -v curl >/dev/null 2>&1 && command -v git >/dev/null 2>&1 \
  || { apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -yq curl git ca-certificates </dev/null; }
curl -fsSL https://raw.githubusercontent.com/narasitk77/live-clip/main/cloud/setup.sh | sh
cd /opt/live-clip

gen() { tr -dc 'a-z0-9' </dev/urandom | head -c "$1"; }
setenv() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >>.env; fi; }
val() { sed -n "s/^$1=//p" .env | tail -n 1; }
setenv PUBLIC_HOST "$HOST"
setenv SRT_PUBLIC_HOST "$HOST"
setenv IMAGE_TAG sha-17b28b5   # the image docker-compose.rtmp.yml was tested with on 2 Nov 2026
setenv COMPOSE_FILE docker-compose.cloud.yml:docker-compose.rtmp.yml
for v in MAIN_PUBLISH_PASS EXPERTISE_PUBLISH_PASS RELAY_PUBLISH_PASS; do
  [ -n "$(val "$v")" ] || setenv "$v" "$(gen 32)"
done
chmod 600 .env

IP=$(curl -fsS https://api.ipify.org)
SRTP=$(val SRT_PASSPHRASE)
umask 077
{
  echo "live-clip senders: $HOST ($IP). Secret - send each stage only its own block, never in a group chat."
  echo "Video H.264 (never HEVC/AV1) + AAC stereo, keyframe every 2 s, one encoder per key."
  for s in main expertise; do
    p=$(val "$(echo "$s" | tr a-z A-Z)_PUBLISH_PASS")
    echo
    echo "== $s =="
    echo "RTMP  Server:     rtmp://$HOST:1935      (until DNS works: rtmp://$IP:1935)"
    echo "      Stream key: $s?user=$s&pass=$p"
    echo "      (Server without /live; leave the encoder's username/password fields empty)"
    echo "SRT fallback, only if the venue blocks TCP 1935:"
    echo "  OBS/ffmpeg URL: srt://$HOST:8890?streamid=publish:$s:$s:$p&passphrase=$SRTP&latency=2000000"
    echo "  vMix fields:    Caller, Hostname $HOST, Port 8890, Latency 2000 ms, Passphrase $SRTP,"
    echo "                  Key length 16, Stream ID publish:$s:$s:$p"
    echo "  Wirecast: use RTMP (its SRT has no latency setting)"
  done
  echo
  echo "== WhistleCutter: Live > tab 'จาก Stream' =="
  echo "URL https://$HOST (type https:// yourself; without it the password crosses in clear text)"
  echo "user $(val WEB_USER)   password $(val WEB_PASSWORD)"
  echo "Before DNS works: ssh tunnel (docs/runbook-cloud-rtmp.md) and use http://127.0.0.1:18080"
} >/root/live-clip-senders.txt

echo "pulling ~220 MB of images (several minutes on the 4 Mbps international link), do not Ctrl-C"
docker compose pull mediamtx web caddy
docker compose up -d mediamtx web
# Caddy starts only after DNS, so prove its config parses now rather than on test day.
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
  || { echo "FAIL: cloud/Caddyfile does not validate: docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" >&2; exit 1; }

# Loud checks: RTMP listening, the web app can read MediaMTX (API user still at index 2), disk policy.
i=0
until ss -ltn | grep -q ':1935 '; do
  i=$((i + 1)); [ $i -lt 30 ] || { echo "FAIL: nothing listens on TCP 1935. docker compose logs mediamtx" >&2; exit 1; }
  sleep 1
done
docker compose exec -T web python3 - <<'EOF'
import base64, json, os, sys, time, urllib.request
auth = 'Basic ' + base64.b64encode(f"{os.environ['WEB_USER']}:{os.environ['WEB_PASSWORD']}".encode()).decode()
def call(path, body=None):
    req = urllib.request.Request('http://127.0.0.1:8080' + path, headers={'Authorization': auth},
                                 data=None if body is None else json.dumps(body).encode())
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)
err = None
for _ in range(30):
    try:
        state = call('/api/state')
        err = [w for w in state['warnings'] if 'MediaMTX' in w]
        if not err:
            break
    except OSError as e:  # web not up yet, or 401
        err = repr(e)
    time.sleep(1)
else:
    sys.exit(f'FAIL: web check: {err}  (docker compose logs web mediamtx)')
if state['settings']['keep_hours'] == 24:  # untouched default; 80 GB cannot hold 24 h of ended sessions
    call('/api/settings', {'keep_hours': 8})
print(f"web OK: disk {state['disk']['free_gb']}/{state['disk']['total_gb']} GB free, keep ended sessions 8 h (change in the web UI)")
EOF

[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] \
  || echo "WARN: clock not NTP-synced yet (timedatectl). Buffer timestamps come from this clock."
if getent ahostsv4 "$HOST" | grep -q "^$IP "; then
  echo "DNS: $HOST -> $IP. Start HTTPS now: cd /opt/live-clip && docker compose up -d"
else
  echo "DNS: $HOST does not point at $IP yet (normal before IT adds it). Do NOT start Caddy until it does."
fi
echo "Sender settings + WhistleCutter login: /root/live-clip-senders.txt (secret)."
}

main "$@"
