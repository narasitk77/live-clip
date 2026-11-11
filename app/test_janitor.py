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

# ---- delete: an editor removes a clip for good ----
import base64, http.client, threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


def refused(fn, kind):
    try:
        fn()
    except kind:
        return True
    return False


G, H, I, J, K, L, M, N = (ch * 32 for ch in '12345678')
O, P, Q, R = ('0' * 31 + ch for ch in '1234')   # ids no other check uses
clip(G, 'done', None, 60, storage='local')
assert app.delete_clip(G) == {'ok': True, 'wasabi_kept': None}
assert not app.clip_path(G, 'json').exists() and not app.clip_path(G, 'mp4').exists(), 'record and file are both gone'
assert G not in [c['id'] for c in app.load_clips()], 'gone from the list everyone sees'
clip(H, 'done', 'k/h.mp4', 60)
assert app.delete_clip(H)['wasabi_kept'] == 'k/h.mp4', 'the reply names the copy that stays on Wasabi'

clip(I, 'cutting', None, 5, local=False)
assert refused(lambda: app.delete_clip(I), app.ClipError) and state(I)['status'] == 'cutting', 'a clip being cut is not deletable'
clip(J, 'failed', None, 60)
with app.LOCK:
    app.RUNNING.add((J, 'cut'))          # a retry was queued a moment ago
assert refused(lambda: app.delete_clip(J), app.ClipError) and app.clip_path(J, 'mp4').exists(), 'a queued job keeps its clip'
with app.LOCK:
    app.RUNNING.discard((J, 'cut'))

stale = state(J)                         # what a retry or the janitor read before the delete
app.delete_clip(J)
assert refused(lambda: app.update_clip(stale, status='queued'), app.BadRequest), 'a late writer is told the clip is gone'
assert refused(lambda: app.retry_clip(J), app.BadRequest)
assert not app.clip_path(J, 'json').exists(), 'a deleted clip is never written back into the list'

app.clip_path(K, 'mp4').write_bytes(b'mp4')   # the janitor still holds the record of a clip deleted meanwhile
real = app.load_clips
app.load_clips = lambda: [{'id': K, 'status': 'done', 's3_key': 'k/k.mp4', 'created': NOW - 30 * 3600, 'local': True}]
try:
    app.janitor_once(NOW)                # must not raise, must not bring the record back
finally:
    app.load_clips = real
assert not app.clip_path(K, 'json').exists()

app.clip_path(L, 'json').write_text('{not json')
app.clip_path(L, 'mp4').write_bytes(b'x')
assert app.delete_clip(L)['ok'] and not app.clip_path(L, 'json').exists() and not app.clip_path(L, 'mp4').exists(), \
    'a record nobody can read is deletable'

clip(M, 'done', None, 60, storage='local')
app.clip_path(M, 'mp4').unlink()
app.clip_path(M, 'mp4').mkdir()
(app.clip_path(M, 'mp4') / 'x').write_text('')     # stands in for a file the server cannot remove
assert refused(lambda: app.delete_clip(M), OSError) and app.clip_path(M, 'json').exists(), \
    'file not removed: the record stays, so the clip is still listed to try again'

for bad in ('../../etc/passwd', '', 'A' * 32, G[:31], None, '9' * 32):
    assert refused(lambda: app.delete_clip(bad), app.BadRequest), bad
(tmp / 'evil.json').write_text('{}')     # a record-shaped file one level above the clips folder
(tmp / 'evil.mp4').write_bytes(b'x')
assert refused(lambda: app.delete_clip('../evil'), app.BadRequest) and (tmp / 'evil.json').exists() and (tmp / 'evil.mp4').exists(), \
    'an id is never a path: nothing outside the clips folder can be removed'
assert app.clip_path(B, 'mp4').exists() and app.clip_path(D, 'mp4').exists(), 'other clips untouched'

clip(O, 'done', None, 60, storage='local')
(app.CLIPS / f'tmp-{O}').mkdir()
(app.CLIPS / f'tmp-{O}' / 'seg_1.ts').write_bytes(b'x')
assert refused(lambda: app.delete_clip(O, 'failed'), app.ClipError) and app.clip_path(O, 'mp4').exists(), \
    'the row said failed, the clip is done now (someone retried it): not deleted on the strength of the old row'
assert app.delete_clip(O, 'done')['ok'] and not (app.CLIPS / f'tmp-{O}').exists(), 'the work dir goes with the clip'

os.symlink(app.CLIPS / 'nowhere.json', app.clip_path(P, 'json'))   # stands in for a record deleted between the listing and the read
assert P not in [c['id'] for c in app.load_clips()], 'a record that vanished mid-listing is not shown as a damaged clip'
app.clip_path(P, 'json').unlink()


class Pool:                              # holds jobs instead of running them
    def __init__(self):
        self.jobs = []

    def submit(self, *a):
        self.jobs.append(a)


clip(Q, 'failed', None, 60, storage='local')
with app.LOCK:
    app.RUNNING.add((Q, 'cut'))          # the failed job has written 'failed' but not left RUNNING yet
assert refused(lambda: app.retry_clip(Q), app.BadRequest) and state(Q)['status'] == 'failed', 'no second job while the first is closing'
with app.LOCK:
    app.RUNNING.clear()
cut, up = app.CUT_POOL, app.UPLOAD_POOL
app.CUT_POOL = app.UPLOAD_POOL = pool = Pool()
try:
    assert app.retry_clip(Q)['status'] == 'queued'
    assert refused(lambda: app.retry_clip(Q), app.BadRequest), 'a second retry (double click, two editors) is refused'
    assert len(pool.jobs) == 1 and [c for c, _ in app.RUNNING if c == Q] == [Q], 'one clip, one job'
    assert refused(lambda: app.delete_clip(Q), app.ClipError), 'and that job protects the clip from delete'
finally:
    app.CUT_POOL, app.UPLOAD_POOL = cut, up
    with app.LOCK:
        app.RUNNING.clear()

rec = {'id': R, 'name': 'อัปค้าง', 'stream': 'cam1', 't_in': NOW, 'status': 'failed', 'failed_stage': 'upload',
       'storage': 'wasabi', 's3_key': None, 'local': True, 'created': NOW}
app.save_clip(rec)
app.clip_path(R, 'mp4').write_bytes(b'mp4')
assert app.delete_clip(R) == {'ok': True, 'wasabi_kept': None, 'wasabi_maybe': app.s3_key(rec)}, \
    'an upload that may have landed without being recorded: the reply says where to look'

app.WEB_USER, app.WEB_PASSWORD = 'editor', 'x' * 16
srv = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(path, auth=b'editor:' + b'x' * 16, body='{}'):
    h = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=10)
    h.request('POST', path, body, {'Content-Type': 'application/json', 'Authorization': 'Basic ' + base64.b64encode(auth).decode()})
    r = h.getresponse()
    body = r.read()
    return r.status, (json.loads(body) if r.status != 401 else None)


clip(N, 'done', None, 60, storage='local')
assert post(f'/api/clips/{N}/delete', b'editor:wrong')[0] == 401 and app.clip_path(N, 'mp4').exists(), 'no login, no delete'
assert post(f'/api/clips/{N}/delete') == (200, {'ok': True, 'wasabi_kept': None})
assert post(f'/api/clips/{N}/delete')[0] == 400, 'second delete: the clip is gone'
clip(N, 'uploading', None, 5)
assert post(f'/api/clips/{N}/delete')[0] == 409 and app.clip_path(N, 'mp4').exists(), 'busy clip: refused over HTTP too'
clip(N, 'done', None, 60, storage='local')
assert post(f'/api/clips/{N}/delete', body='{"status": "failed"}')[0] == 409 and app.clip_path(N, 'mp4').exists(), \
    'the page sends the status its row showed; a stale row does not delete'

# A body the server never reads (here: on a GET without login) must not be parsed as the next
# request on the same connection, where it would run with the next sender's login.
import socket  # noqa: E402
smuggled = f'POST /api/clips/{N}/delete HTTP/1.1\r\nContent-Type: application/json\r\nX-Pad: '.encode()
login = base64.b64encode(b'editor:' + b'x' * 16).decode()
got = b''
with socket.create_connection(('127.0.0.1', srv.server_port), timeout=5) as sk:
    sk.sendall(f'GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: {len(smuggled)}\r\n\r\n'.encode() + smuggled)
    try:
        sk.sendall(f'GET /api/clips HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {login}\r\n\r\n'.encode())
        while True:
            chunk = sk.recv(65536)
            if not chunk:
                break
            got += chunk
    except (ConnectionResetError, BrokenPipeError):
        pass                             # the server hung up first: that is the point
    except socket.timeout:
        pass                             # it kept the connection open: the checks below say what that means
assert got.count(b'HTTP/1.') <= 1 and (not got or b'Connection: close' in got), 'the connection ends after a request that declared a body'
assert app.clip_path(N, 'json').exists() and app.clip_path(N, 'mp4').exists(), 'the smuggled delete never ran'
srv.shutdown()
print('test_janitor: all checks passed')
