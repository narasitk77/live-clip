"""Self-check for the janitor and job bookkeeping (deletes data, so it runs on a throwaway
DATA_DIR). Run: python test_janitor.py  (no ffmpeg needed)"""
import json, os, tempfile, time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ['DATA_DIR'] = str(tmp)
import app  # noqa: E402  (reads DATA_DIR at import)

NOW = time.time()


def age(path, seconds):
    os.utime(path, (NOW - seconds, NOW - seconds))


def session(stream, sid, age_s, segs=3, extra=()):
    d = app.DVR / stream / sid
    d.mkdir(parents=True)
    names = [f'seg_{i:08d}.ts' for i in range(segs)]
    for n in list(names) + list(extra):
        (d / n).write_bytes(b'x')
        age(d / n, age_s)
    pdt = '2026-10-21T07:00:00.000+0000'
    body = '#EXTM3U\n' + ''.join(f'#EXTINF:2.0,\n#EXT-X-PROGRAM-DATE-TIME:{pdt}\n{n}\n' for n in names)
    (d / 'index.m3u8').write_text(body if segs else '')
    age(d / 'index.m3u8', age_s)
    app.read_segments(d) if segs else None   # fill the playlist cache like the UI would
    return d


def clip(cid, status, s3_key, age_s, local=True, storage='wasabi'):
    c = {'id': cid, 'name': cid, 'status': status, 's3_key': s3_key, 'created': NOW - age_s,
         'local': local, 'storage': storage}
    app.save_clip(c)
    if local:
        app.clip_path(cid, 'mp4').write_bytes(b'mp4')
    return c


def state(cid):
    return json.loads(app.clip_path(cid, 'json').read_text())


for p in (app.DVR, app.CLIPS, app.SETTINGS):
    p.mkdir(parents=True, exist_ok=True)

expired = session('cam1', '20261019-000000', 25 * 3600)
ended = session('cam1', '20261021-000000', 3600)                    # past the reconnect window
reconnecting = session('cam4', '20261021-030000', 30)                # encoder dropped 30 s ago
live = session('cam2', '20261021-010000', 0, extra=['seg_00000099.ts'])  # 99 = fresh, unreferenced
orphan = live / 'seg_00000050.ts'
orphan.write_bytes(b'x'); age(orphan, 300)
broken = session('cam3', '20261021-020000', 900, segs=0, extra=['seg_00000001.ts', 'seg_00000002.ts'])
A, B, C, D, E, F = (ch * 32 for ch in 'abcdef')
clip(A, 'done', 'k/a.mp4', 30 * 3600)                        # on Wasabi, old -> local copy removed
clip(B, 'failed', None, 30 * 3600)                           # never uploaded -> keep its only copy
clip(C, 'cutting', None, 10, local=False)                    # interrupted cut
clip(D, 'done', None, 30 * 24 * 3600, storage='local')       # local-only mode: the only copy, keep
clip(E, 'uploading', None, 10)                               # cut finished, upload interrupted
clip(F, 'cutting', None, 10, local=False); age(app.clip_path(F, 'json'), 600)  # job died silently

app.janitor_once(NOW)
assert not expired.exists(), 'session older than keep_hours removed'
assert ended.exists() and reconnecting.exists(), 'ended sessions kept while disk is fine'
assert not orphan.exists(), 'old unreferenced segment removed'
assert (live / 'seg_00000099.ts').exists(), 'fresh unreferenced segment kept (may be in flight)'
assert all((live / f'seg_{i:08d}.ts').exists() for i in range(3)), 'referenced segments kept'
assert (broken / 'seg_00000001.ts').exists() and (broken / 'seg_00000002.ts').exists(), 'empty playlist: delete nothing'
assert any('รายการว่าง' in w for w in app.WARNINGS.values()), 'empty playlist raises a warning'
assert not app.clip_path(A, 'mp4').exists() and state(A)['local'] is False
assert app.clip_path(B, 'mp4').exists(), 'clip that never reached Wasabi keeps its only copy'
assert app.clip_path(D, 'mp4').exists() and state(D)['status'] == 'done', 'local-only clips are never expired'
assert state(F)['status'] == 'failed' and state(F)['failed_stage'] == 'cut', 'silently dead job is surfaced'
assert state(C)['status'] == 'cutting', 'a job updated recently is not swept'
assert str(expired / 'index.m3u8') not in app._playlist_cache, 'cache entry of a removed session dropped'

app.recover_interrupted()
assert state(C)['status'] == 'failed' and state(C)['failed_stage'] == 'cut'
assert state(E)['status'] == 'failed' and state(E)['failed_stage'] == 'upload', 'finished cut is never cut again'

# disk guard: pretend the disk is nearly full -> only sessions past the reconnect window go
app.MIN_FREE_GB = 1e12
app.janitor_once(NOW)
assert not ended.exists() and not broken.exists(), 'ended sessions removed under disk pressure'
assert reconnecting.exists(), 'a session whose encoder may still reconnect is kept'
assert live.exists(), 'live session never removed by the disk guard'
assert 'disk' in app.WARNINGS and 'disk_early' in app.WARNINGS, 'disk pressure and early removals are shown'
assert not any(k.startswith('empty:') for k in app.WARNINGS), 'warning cleared once its session is gone'
print('test_janitor: all checks passed')
