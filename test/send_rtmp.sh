#!/bin/bash
# Fake stage encoder over RTMP: test pattern showing its own timestamp (1/100 s) + 1 kHz tone,
# H.264/AAC 4 Mbps, keyframe 2 s.
# Usage: test/send_rtmp.sh <server> <stream key> [seconds]
#   test/send_rtmp.sh rtmp://liveclip.thestandard.co:1935 'xstage?user=xstage&pass=...' 600
# Quote the key ('&' splits a shell command). It holds the stage password, so every line ffmpeg
# prints goes through a mask. Uses ffmpeg on PATH, else the one inside ~/WhistleCutter.
set -o pipefail
server=${1:?server, e.g. rtmp://HOST:1935}; key=${2:?stream key, e.g. main?user=main&pass=...}; secs=${3:-600}
ff=$(command -v ffmpeg || echo "$HOME/WhistleCutter/ffmpeg")
echo "sending ${secs}s to ${server}/${key%%\?*} ... (Ctrl-C to stop)"
"$ff" -hide_banner -nostats -loglevel warning -re \
  -f lavfi -i testsrc=size=1280x720:rate=30:decimals=2 -f lavfi -i sine=frequency=1000:sample_rate=48000 \
  -c:v libx264 -preset veryfast -b:v 4M -maxrate 4M -bufsize 8M -g 60 -keyint_min 60 -sc_threshold 0 \
  -pix_fmt yuv420p -c:a aac -b:a 128k -t "$secs" -f flv "$server/$key" 2>&1 \
  | awk '{ gsub(/pass=[^&" ]*/, "pass=***"); print; fflush() }'
rc=$?
[ $rc -eq 0 ] && echo "done" || echo "FAILED (exit $rc): 'authentication failed' = wrong key/password, 'Connection refused' = port 1935 closed"
exit $rc
