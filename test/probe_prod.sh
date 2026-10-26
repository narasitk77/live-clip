#!/bin/sh
# Send a 15 s test pattern over SRT to the production receiver, from wherever this runs.
# Reads SRT_PASSPHRASE / SRT_PUBLIC_HOST from .env.portainer; never prints the passphrase.
# Success = "publishing to path 'probe1'" in: python3 scripts/portainer_stack.py logs mediamtx
cd "$(dirname "$0")/.." || exit 1
PP=$(grep '^SRT_PASSPHRASE=' .env.portainer | cut -d= -f2-); HOST=$(grep '^SRT_PUBLIC_HOST=' .env.portainer | cut -d= -f2-)
docker run --rm --entrypoint timeout bluenviron/mediamtx:1.21.2-ffmpeg 25 ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i testsrc=size=640x360:rate=25 -f lavfi -i sine=frequency=1000:sample_rate=48000 -t 15 \
  -c:v libx264 -preset veryfast -g 50 -pix_fmt yuv420p -c:a aac \
  -f mpegts "srt://$HOST:8890?streamid=publish:probe1&passphrase=$PP&pbkeylen=16&latency=1000000&connect_timeout=8000" 2>&1 \
  | sed "s/passphrase=[^&]*/passphrase=***/g"
echo "exit=$? (0 = 15 s sent; 'I/O error' = port not reachable or passphrase wrong)"
