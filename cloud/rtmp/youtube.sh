#!/bin/sh
# Started by MediaMTX (runOnOnline, see mediamtx.yml) for every live stage; stopped with SIGINT to
# the whole process group when the stage goes offline. Pushes the stage, untouched (-c copy, so no
# CPU and no quality loss), to YouTube while /data/youtube/<stage>.key exists. No key file: it only
# idles, so a stage nobody set up never goes out. cloud/set-youtube-key.sh writes or removes the
# file from the Mac (the key is typed there, never in git, env or chat).
# The file holds a YouTube stream key (xxxx-xxxx-xxxx-xxxx-xxxx) or a whole rtmp(s):// URL for
# another platform. Bandwidth: one stage's bitrate (about 6 Mbps) on the international link.
# /data/youtube/<stage>.status (no secrets) tells the web page and set-youtube-key.sh how it goes:
#   pushing <epoch>                    ffmpeg started
#   failing <epoch> <last log line>    ffmpeg gave up within 30 s (bad key, no live event, network)
#   stopped <epoch>                    a push that had been running ended; retrying
set -u
: "${MTX_PATH:?MTX_PATH not set}"
case "$MTX_PATH" in ''|*[!a-z0-9_-]*) echo "youtube: refusing odd stream name '$MTX_PATH'" >&2; exit 1 ;; esac
keyfile=/data/youtube/$MTX_PATH.key
status=/data/youtube/$MTX_PATH.status
idle=
while :; do
  if [ ! -s "$keyfile" ]; then
    [ -n "$idle" ] || echo "youtube: $MTX_PATH has no key file, not pushing" >&2
    idle=1
    sleep 5
    continue
  fi
  idle=
  dest=$(tr -d ' \r\n' < "$keyfile")
  case "$dest" in
    rtmp://* | rtmps://*) ;;
    *) dest="rtmps://a.rtmps.youtube.com:443/live2/$dest" ;;
  esac
  # ffmpeg 8 does not check TLS certificates unless told to: without this anyone answering for the
  # host name (DNS spoof, interception) would receive the stream key. Only rtmps knows the option.
  tls=
  case "$dest" in rtmps://*) tls='-tls_verify 1' ;; esac
  echo "youtube: $MTX_PATH pushing" >&2
  printf 'pushing %s\n' "$(date +%s)" > "$status"
  start=$(date +%s)
  # Reads back over SRT from localhost like dvr.sh (user 1). rw_timeout: a connection that goes
  # silent fails within 10 s instead of hanging in a TCP write for many minutes. The awk hides the
  # destination (ffmpeg prints it, key included, on any error) and keeps the last line for the status.
  # shellcheck disable=SC2086
  ffmpeg -nostdin -hide_banner -loglevel warning \
    -i "srt://127.0.0.1:8890?streamid=read:$MTX_PATH" \
    -map '0:v?' -map '0:a?' -c copy -f flv $tls -rw_timeout 10000000 "$dest" 2>&1 \
    | awk -v p="$MTX_PATH" -v last="$status.last" '{ gsub(/rtmps?:\/\/[^ "]*/, "<destination>"); print "youtube: " p ": " $0; fflush(); print > last; close(last) }' >&2
  if [ $(( $(date +%s) - start )) -lt 30 ]; then
    printf 'failing %s %s\n' "$(date +%s)" "$(tail -n 1 "$status.last" 2>/dev/null)" > "$status"
  else
    printf 'stopped %s\n' "$(date +%s)" > "$status"
  fi
  echo "youtube: $MTX_PATH push stopped, retrying in 5 s" >&2
  sleep 5
done
