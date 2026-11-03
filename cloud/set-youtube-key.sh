#!/bin/bash
# Turn the YouTube push of one stage on or off, from this Mac:
#   cloud/set-youtube-key.sh <VM IP> <stage>          asks for the stream key (hidden), starts pushing
#   cloud/set-youtube-key.sh <VM IP> <stage> --off    stops pushing that stage
#   cloud/set-youtube-key.sh <VM IP> - --status       which stages are armed and how their push goes
# The key travels to the VM on ssh stdin only and lands in /data/youtube/<stage>.key (0600) for
# cloud/rtmp/youtube.sh. Nothing restarts: MediaMTX and both stages keep running; only the push
# for that stage (re)starts, within ~5 s, and only while the stage is live.
# A whole rtmp(s):// URL is accepted instead of a key, for a platform other than YouTube.
set -euo pipefail
ip=${1:?VM IP}; stage=${2:?stage, e.g. main or xstage}; mode=${3:-}
ssh=(ssh -o BatchMode=yes root@"$ip")
[ -f ~/.ssh/liveclip_ed25519 ] && ssh=(ssh -i ~/.ssh/liveclip_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes root@"$ip")
# Run a sh script inside the mediamtx container. The script crosses the remote login shell inside
# single quotes (its own single quotes escaped), so neither shell on the way expands anything in it.
# stdin passes through: the ON path sends the key that way.
inbox() { local q=${1//\'/\'\\\'\'}; "${ssh[@]}" "cd /opt/live-clip && docker compose exec -T mediamtx sh -c '$q'"; }

if [ "$mode" = --status ]; then
  inbox 'cd /data/youtube 2>/dev/null || { echo "no stage has ever been armed"; exit 0; }
    now=$(date +%s)
    for k in *.key; do [ -e "$k" ] || continue; s=${k%.key}
      st=$(cat "$s.status" 2>/dev/null || echo "never ran (stage not live since it was armed)")
      set -- $st; [ $# -ge 2 ] && st="$st  ($(( now - $2 )) s ago)"
      echo "$s: ARMED  $st"; done
    ls *.key >/dev/null 2>&1 || echo "no stage armed"'
  exit 0
fi
[[ $stage =~ ^[a-z0-9_-]{1,32}$ ]] || { echo "FAIL: stage: a-z 0-9 - _ only" >&2; exit 1; }
# The running push of this stage only: its ffmpeg reads streamid=read:<stage> and writes flv
# (dvr.sh writes hls). fl[v] keeps the pattern from matching the sh -c that runs it.
kick="{ pkill -f 'streamid=read:$stage .*-f fl[v]' || true; }"

if [ "$mode" = --off ]; then
  # kick only once the file is really gone, so a failed rm cannot report OFF and keep pushing
  inbox "rm -f /data/youtube/$stage.key && ! test -e /data/youtube/$stage.key && $kick"
  echo "YouTube push for $stage: OFF"
  exit 0
fi
[ -z "$mode" ] || { echo "FAIL: third argument can only be --off or --status" >&2; exit 1; }

read -rsp "YouTube stream key for $stage (or a full rtmp(s):// URL): " key; echo
key=$(printf '%s' "$key" | tr -d ' \r\n')
[[ $key =~ ^[A-Za-z0-9-]{8,64}$ || $key =~ ^rtmps?://[^[:space:]]+$ ]] \
  || { echo "FAIL: does not look like a stream key (letters, digits, dashes) or an rtmp(s):// URL, nothing changed" >&2; exit 1; }
printf '%s\n' "$key" | inbox "umask 077; mkdir -p /data/youtube && cat > /data/youtube/$stage.key.tmp && mv /data/youtube/$stage.key.tmp /data/youtube/$stage.key && rm -f /data/youtube/$stage.status && $kick"
echo "YouTube push for $stage: armed. Checking for 25 s whether it actually goes out..."
sleep 25
st=$(inbox "cat /data/youtube/$stage.status 2>/dev/null; true")
now=$(date +%s)
case "$st" in
  pushing\ *)
    t=$(echo "$st" | awk '{print $2}')
    if [ $(( now - t )) -ge 15 ]; then echo "OK: $stage is pushing (for $(( now - t )) s). Confirm in YouTube Studio > Stream health."
    else echo "STARTING: $stage push started $(( now - t )) s ago; re-check with: $0 $ip - --status"; fi ;;
  failing\ *)
    echo "FAIL: $stage push is being refused: ${st#failing * }" >&2
    echo "      Check the key and that the live event exists in YouTube Studio. Stop with: $0 $ip $stage --off" >&2
    exit 1 ;;
  *)
    echo "NOT VERIFIED: $stage is not live right now, so nothing was sent yet. It starts by itself when"
    echo "the stage goes live; check then with: $0 $ip - --status" ;;
esac
