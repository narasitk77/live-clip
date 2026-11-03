#!/bin/bash
# The whole first test from this Mac, once Hostatom's email arrives (asks for the root password once):
#   cloud/up-from-mac.sh <VM IP> [hostname, default <ip-with-dashes>.sslip.io, which needs no DNS work]
# 1. runs cloud/rtmp-setup.sh on the VM   2. copies the sender sheet to ./senders-cloud.txt (0600, gitignored)
# 3. sends a 15-minute RTMP test pattern as stage "xstage"   4. opens the web tunnel on 127.0.0.1:18080
# Then in WhistleCutter: Live > จาก Stream > http://127.0.0.1:18080. Ctrl-C closes the tunnel and the test.
set -euo pipefail
ip=${1:?VM IP from the Hostatom email}; host=${2:-$(echo "$ip" | tr . -).sslip.io}
cd "$(dirname "$0")/.."
mkdir -p ~/.ssh
# One ssh connection reused by every step below, so the password is typed once. accept-new trusts the
# brand-new VM's host key on first contact only.
ssh=(ssh -o ControlMaster=auto -o ControlPath="$HOME/.ssh/lc-%C" -o ControlPersist=20m -o StrictHostKeyChecking=accept-new)

"${ssh[@]}" root@"$ip" "curl -fsSL https://raw.githubusercontent.com/narasitk77/live-clip/main/cloud/rtmp-setup.sh | sh -s $host"

(umask 077; "${ssh[@]}" root@"$ip" cat /root/live-clip-senders.txt >senders-cloud.txt)
key=$(sed -n 's/^ *Stream key: \(xstage?.*\)$/\1/p' senders-cloud.txt)
[ -n "$key" ] || { echo "FAIL: no xstage key in senders-cloud.txt" >&2; exit 1; }
web=$("${ssh[@]}" root@"$ip" "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' live-clip-web")

log=${TMPDIR:-/tmp}/live-clip-send.log
test/send_rtmp.sh "rtmp://$ip:1935" "$key" 900 >"$log" 2>&1 &
sender=$!
trap 'kill $sender 2>/dev/null' EXIT
echo
echo "sender sheet (secret): $PWD/senders-cloud.txt  - WhistleCutter user/password are at the bottom"
echo "test pattern -> stage xstage for 15 min (log: $log)"
echo "web: http://127.0.0.1:18080   WhistleCutter: Live > จาก Stream > http://127.0.0.1:18080"
echo "Ctrl-C to close the tunnel and stop the test"
"${ssh[@]}" -N -L 18080:"$web":8080 root@"$ip"
