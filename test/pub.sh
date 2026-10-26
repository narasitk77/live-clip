#!/bin/sh
# Fake vMix: test pattern + tone over SRT. Usage: test/pub.sh <name> [seconds] [passphrase]
name=${1:-cam1}; secs=${2:-1800}; pass=${3:-$SRT_PASSPHRASE}
docker rm -f "pub-$name" >/dev/null 2>&1
docker run -d --name "pub-$name" --network liveclip_default --entrypoint ffmpeg bluenviron/mediamtx:1.21.2-ffmpeg \
  -hide_banner -loglevel warning -re -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=1000:sample_rate=48000 \
  -c:v libx264 -preset veryfast -b:v 4M -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p -c:a aac -b:a 128k -t "$secs" \
  -f mpegts "srt://mediamtx:8890?streamid=publish:$name&passphrase=$pass&pbkeylen=16&latency=1000000" >/dev/null
