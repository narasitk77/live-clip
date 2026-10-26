#!/bin/sh
# Started by MediaMTX (runOnAvailable) for each live stream. Writes a rolling HLS buffer to
# /data/dvr/<stream>/<session>/ that the web app plays and cuts clips from.
# A reconnect within RECONNECT_MIN minutes continues the same session (same timeline).
#
# Signals: MediaMTX stops this hook with ONE SIGINT to the whole process group, so ffmpeg
# gets it directly. Never forward a second signal to ffmpeg: a second signal while it
# writes its final playlist leaves index.m3u8 at 0 bytes (reproduced), which loses the
# whole buffer on the next reconnect.
set -eu
: "${MTX_PATH:?MTX_PATH not set}"
case "$MTX_PATH" in ''|*[!a-z0-9_-]*) echo "dvr: refusing odd stream name '$MTX_PATH'" >&2; exit 1 ;; esac

SEG=2
RECONNECT_MIN=${RECONNECT_MIN:-10}
SETTING=/data/settings/buffer_minutes
read_buffer() { b=$(cat "$SETTING" 2>/dev/null || echo 120); case "$b" in ''|*[!0-9]*) echo 120 ;; *) echo "$b" ;; esac; }

root=/data/dvr/$MTX_PATH
mkdir -p "$root"

# MediaMTX does not wait for the previous hook to exit before starting a new one, so on a
# fast reconnect the old ffmpeg may still be writing. Wait for it (ffmpeg inherits fd 9).
# (busybox flock has no -w, so poll with -n)
exec 9>"$root/.lock"
tries=0
until flock -n 9; do
  tries=$((tries + 1))
  if [ "$tries" -ge 30 ]; then
    echo "dvr: previous recorder for $MTX_PATH still running after 30 s, will retry" >&2
    exit 1
  fi
  sleep 1
done
[ "$tries" -gt 0 ] && echo "dvr: $MTX_PATH waited ${tries}s for the previous recorder to finish" >&2

last=$(ls -1 "$root" | grep -E '^[0-9]{8}-[0-9]{6}$' | sort | tail -n 1 || true)
dir=
if [ -n "$last" ] && [ -n "$(find "$root/$last/index.m3u8" -mmin "-$RECONNECT_MIN" 2>/dev/null)" ]; then
  if grep -q '^seg_' "$root/$last/index.m3u8"; then
    dir=$root/$last
    echo "dvr: $MTX_PATH continues session $last" >&2
  else
    # never append onto an empty/broken playlist: ffmpeg would restart numbering and
    # overwrite the segments that are still on disk
    echo "dvr: $MTX_PATH session $last has an empty playlist, starting a new session" >&2
  fi
fi
if [ -z "$dir" ]; then
  dir=$root/$(date -u +%Y%m%d-%H%M%S)
  [ -e "$dir" ] && sleep 1 && dir=$root/$(date -u +%Y%m%d-%H%M%S)
  mkdir -p "$dir"
  echo "dvr: $MTX_PATH new session $(basename "$dir")" >&2
fi

buf=$(read_buffer)
# Probe long enough to see the first keyframe of a 2 s GOP (1 s missed it with OBS). The
# probe time also shifts PROGRAM-DATE-TIME later by the same amount; player and cutter both
# use those same timestamps, so clips stay exact.
ffmpeg -nostdin -hide_banner -loglevel warning \
  -analyzeduration 3000000 -probesize 10000000 \
  -i "srt://127.0.0.1:8890?streamid=read:$MTX_PATH" \
  -map '0:v?' -map '0:a?' -c copy \
  -f hls -hls_time "$SEG" -hls_list_size $((buf * 60 / SEG)) \
  -hls_flags delete_segments+append_list+program_date_time+omit_endlist+temp_file+independent_segments \
  -hls_segment_filename "$dir/seg_%08d.ts" "$dir/index.m3u8" &
pid=$!
stop=0
trap 'stop=1' INT TERM

# Buffer length changed in the web UI: restart ffmpeg with the new window (MediaMTX
# restarts this script because runOnAvailableRestart is on). Costs a few seconds of picture.
while [ "$stop" = 0 ] && kill -0 "$pid" 2>/dev/null; do
  sleep 1 &
  wait $! || true
  now=$(read_buffer)
  if [ "$stop" = 0 ] && [ "$now" != "$buf" ]; then
    echo "dvr: $MTX_PATH buffer $buf -> $now min, restarting recorder" >&2
    kill -INT "$pid" 2>/dev/null || true   # the only signal ffmpeg gets on this path
    break
  fi
done
wait "$pid"
