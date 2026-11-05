"""Self-check for replay (stream an ended round back out). Runs a fake ffmpeg, so no network and
no ffmpeg needed. Run: python test_replay.py"""
import json, os, sys, tempfile, time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ['DATA_DIR'] = str(tmp / 'data')
import app  # noqa: E402  (reads DATA_DIR at import)
from app import BadRequest, ClipError, Seg  # noqa: E402

KEY = 'abcd-efgh-ijkl-mnop'
FAKE = tmp / 'fake_ffmpeg.py'
FAKE.write_text(r'''
import json, os, signal, sys, time
plan = os.environ['FAKE_PLAN'].split(',')
cnt = os.environ['FAKE_COUNT']
n = int(open(cnt).read()) if os.path.exists(cnt) else 0
open(cnt, 'w').write(str(n + 1))
step = plan[min(n, len(plan) - 1)]
with open(os.environ['FAKE_LOG'], 'a') as f:
    f.write(json.dumps({'argv': sys.argv[1:], 'cwd': sorted(os.listdir('.')),
                        'list': open('list.ffconcat').read()}) + '\n')
signal.signal(signal.SIGTERM, lambda *a: sys.exit(255))
dest = sys.argv[-1]
speed = float(os.environ.get('FAKE_SPEED', '1'))   # seconds of picture per real second
total = sum(float(l.split()[1]) for l in open('list.ffconcat') if l.startswith('duration'))
def prog(t):
    print(f'out_time_us={int(t * 1e6)}', file=sys.stderr)
    print('progress=continue', file=sys.stderr, flush=True)
if step == 'fail':                      # refused while opening the output: no progress at all
    print('[tls @ 0x1] IO error: End of file', file=sys.stderr)
    print('[out#0/flv @ 0x2] Error opening output ' + dest + ' I/O error', file=sys.stderr)
    print('Error opening output files: I/O error', file=sys.stderr)
    sys.exit(1)
if step.startswith('run'):              # on air for N real seconds, then the connection drops
    t0 = time.time()
    while time.time() - t0 < float(step[3:]):
        prog((time.time() - t0) * speed)
        time.sleep(0.05)
    print(dest + ': Broken pipe', file=sys.stderr)
    sys.exit(1)
if step == 'hang':
    t0 = time.time()
    while True:
        prog(max(0.05, (time.time() - t0) * speed))
        time.sleep(0.05)
if step == 'short':                     # exits 0 halfway through its list
    prog(total / 2)
    sys.exit(0)
prog(total)                             # 'ok': played the whole list
print('progress=end', file=sys.stderr)
sys.exit(0)
''')
app.REPLAY_FFMPEG = [sys.executable, str(FAKE)]
app.REPLAY_STEADY = 0.5
app.REPLAY_RETRY_WAIT = 0.05
app.REPLAY_REWIND = 0
app.host_addrs = lambda host: {'142.250.66.108'}        # a public address; no DNS in the test
app.stream_params = lambda path: ['same']               # every join has the same format
app.mediamtx_paths = lambda: None                       # MediaMTX unknown unless a test says so
for d in (app.DVR, app.CLIPS, app.SETTINGS, app.REPLAYS):
    d.mkdir(parents=True, exist_ok=True)


def session(stream, sid, age_s, n=5):
    d = app.DVR / stream / sid
    d.mkdir(parents=True)
    names = [f'seg_{i:08d}.ts' for i in range(n)]
    for x in names:
        (d / x).write_bytes(b'ts')
    body = '#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T07:00:00.000+0000\n' + ''.join(
        f'#EXTINF:2.000000,\n{x}\n' for x in names)
    (d / 'index.m3u8').write_text(body)
    t = time.time() - age_s
    os.utime(d / 'index.m3u8', (t, t))
    return d


def plan(steps, speed=1):
    os.environ['FAKE_SPEED'] = str(speed)
    os.environ['FAKE_PLAN'] = steps
    os.environ['FAKE_COUNT'] = str(tmp / f'count-{time.monotonic_ns()}')
    os.environ['FAKE_LOG'] = log = str(tmp / f'log-{time.monotonic_ns()}')
    return log


def calls(log):
    return [json.loads(x) for x in Path(log).read_text().splitlines()]


def wait_final(rid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with app.REPLAY_LOCK:
            running = rid in app.REPLAY_RUN
        rec = json.loads((app.REPLAYS / f'{rid}.json').read_text())
        if not running and rec['status'] in app.REPLAY_FINAL:
            return rec
        time.sleep(0.02)
    raise AssertionError(f'replay {rid} did not end: {rec}')


def statuses(rid, timeout=10):
    """Every status the record shows until it ends (polled, like the page)."""
    seen, end = [], time.time() + timeout
    while time.time() < end:
        st = json.loads((app.REPLAYS / f'{rid}.json').read_text())['status']
        if not seen or seen[-1] != st:
            seen.append(st)
        with app.REPLAY_LOCK:
            if st in app.REPLAY_FINAL and rid not in app.REPLAY_RUN:
                return seen
        time.sleep(0.005)
    raise AssertionError(f'{rid} did not end: {seen}')


def wait_status(rid, status, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if json.loads((app.REPLAYS / f'{rid}.json').read_text())['status'] == status:
            return
        time.sleep(0.02)
    raise AssertionError(f'replay {rid} never reached {status}')


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    return False


def main():
    # ---- destination ----
    assert app.replay_dest(' rtmps://a.rtmps.youtube.com:443/live2/ ', KEY) == \
        (f'rtmps://a.rtmps.youtube.com:443/live2/{KEY}', 'rtmps://a.rtmps.youtube.com:443')
    assert app.replay_dest('rtmp://1.2.3.4/live', 'k1')[0] == 'rtmp://1.2.3.4/live/k1'
    for server in ('http://x/live', 'file:///etc/passwd', 'rtmp://', 'rtmp://ho st/live', 'srt://x:1/a', ''):
        assert raises(BadRequest, app.replay_dest, server, KEY), server
    for key in ('', 'a b', 'x' * 513, None):
        assert raises(BadRequest, app.replay_dest, 'rtmp://h/live', key), key
    real_addrs = app.host_addrs
    app.host_addrs = lambda host: {'mediamtx': {'172.18.0.3'}, 'lan': {'192.168.1.20'}, 'meta': {'169.254.169.254'},
                                   'v6': {'fe80::1%eth0'}}.get(host) or (_ for _ in ()).throw(OSError('no such host'))
    for host in ('mediamtx', 'lan', 'meta', 'v6', 'nosuchhost'):
        assert raises(BadRequest, app.replay_dest, f'rtmp://{host}/live', KEY), host
    app.REPLAY_ALLOW_HOSTS = {'lan'}
    assert app.replay_dest('rtmp://lan/live', KEY)[1] == 'rtmp://lan', 'REPLAY_ALLOW_HOSTS lets a LAN server through'
    app.REPLAY_ALLOW_HOSTS = set()
    app.host_addrs = real_addrs
    line = app.redact(f'[tcp] rtmps://a.rtmps.youtube.com:443/live2/{KEY}: Broken pipe {KEY}', KEY)
    assert KEY not in line and 'rtmp' not in line, line

    # ---- range -> whole segments, list, resume point ----
    segs = [Seg(1000.0 + 2 * i, 2.0, f'seg_{i:08d}.ts') for i in range(5)]
    assert app.replay_segments(segs) == segs
    assert [s.name for s in app.replay_segments(segs, 1003, 1005)] == ['seg_00000001.ts', 'seg_00000002.ts']
    assert raises(ClipError, app.replay_segments, [], None, None)
    assert raises(ClipError, app.replay_segments, segs, 1005, 1003), 'Out before In'
    assert app.replay_segments(segs, 995, 1011.5) == segs, 'points past either end of an ended round clamp'
    assert [s.name for s in app.replay_segments(segs, 1007)] == ['seg_00000003.ts', 'seg_00000004.ts'], 'In only'
    assert [s.name for s in app.replay_segments(segs, None, 1003)] == ['seg_00000000.ts', 'seg_00000001.ts'], 'Out only'
    assert app.replay_index(segs, 0) == (0, 0.0)
    assert app.replay_index(segs, 4.5) == (2, 4.0)
    assert app.replay_index(segs, 99) == (0, 0.0), 'past the end wraps to the start'
    files = lambda text: [x.split("'")[1] for x in text.splitlines() if x.startswith('file ')]  # noqa: E731
    assert files(app.replay_concat(segs, 2, 0)) == [s.name for s in segs[2:]], 'no loop: rest of the range only'
    looped = files(app.replay_concat(segs, 2, True))   # one pass, rotated; ffmpeg -stream_loop repeats it
    assert looped == [f'seg_0000000{i}.ts' for i in (2, 3, 4, 0, 1)], looped

    # ---- a round that is still live is refused; a bad range is refused before anything starts ----
    live = session('xstage', '20261104-070000', 0)
    ended = session('xstage', '20261104-060000', 3600)
    body = {'stream': 'xstage', 'session': ended.name, 'server': 'rtmps://a.rtmps.youtube.com:443/live2', 'key': KEY}
    assert raises(ClipError, app.create_replay, {**body, 'session': live.name})
    assert raises(ClipError, app.create_replay, {**body, 't_in': 1e9, 't_out': 1e9 + 5})
    assert raises(BadRequest, app.create_replay, {**body, 'server': 'http://evil/'})
    app.mediamtx_paths = lambda: {'xstage': {'ready': True}}   # the stage is on air in a newer round
    assert raises(ClipError, app.create_replay, body), 'would be stopped at once by stop_when_live'
    app.mediamtx_paths = lambda: None
    app.stream_params = lambda path: ['1080p'] if path.name < 'seg_00000003.ts' else ['720p']
    joined = session('xstage', '20261104-050000', 3600)
    pl = (joined / 'index.m3u8').read_text().replace('seg_00000003.ts', 'seg_00000003.ts', 1)
    pl = pl.replace('#EXTINF:2.000000,\nseg_00000003.ts', '#EXT-X-PROGRAM-DATE-TIME:2026-11-04T07:05:00.000+0000\n#EXTINF:2.000000,\nseg_00000003.ts')
    (joined / 'index.m3u8').write_text(pl)
    os.utime(joined / 'index.m3u8', (time.time() - 3600, time.time() - 3600))
    assert raises(ClipError, app.create_replay, {**body, 'session': joined.name}), 'a rejoin with another format'
    t0j = app.read_segments(joined)[0].start
    plan('fail')
    assert app.create_replay({**body, 'session': joined.name, 't_out': t0j + 5, 'key': 'probe-ok'}), 'before the join: fine'
    app.stream_params = lambda path: ['same']
    assert not app.REPLAY_RUN or wait_final(next(iter(app.REPLAY_RUN)))
    for p in app.REPLAYS.glob('*.json'):
        p.unlink()
    assert not list(app.REPLAYS.iterdir()) and not app.REPLAY_RUN, 'a refusal leaves nothing behind'

    # ---- bad key: the first run fails at once -> failed, loudly, key never in the record ----
    log = plan('fail')
    rid = app.create_replay({**body, 'loop': True})['id']
    seen = statuses(rid)
    rec = wait_final(rid)
    assert 'streaming' not in seen, f'never "sending" before the destination took it: {seen}'
    assert rec['status'] == 'failed' and 'End of file' in rec['error'] and 'ตรวจ Server' in rec['error'], rec
    assert rec['streamed'] == 0, 'a refused try is not air time' 
    assert KEY not in json.dumps(rec) and 'rtmp' not in rec['error'], rec
    c = calls(log)
    assert len(c) == 1, 'a refused key is not retried'
    argv = c[0]['argv']
    assert argv[-1] == f'rtmps://a.rtmps.youtube.com:443/live2/{KEY}' and '-re' in argv and argv[argv.index('-c') + 1] == 'copy'
    assert argv[argv.index('-stream_loop') + 1] == '-1' and argv.index('-stream_loop') < argv.index('-i'), 'a loop is one connection'
    assert argv[argv.index('-tls_verify') + 1] == '1', 'rtmps checks the certificate'
    assert c[0]['cwd'] == ['list.ffconcat'] + [f'seg_{i:08d}.ts' for i in range(5)], c[0]['cwd']
    assert not (app.REPLAYS / rec['id']).exists(), 'pinned segments removed'

    # ---- one pass, no loop: finished; plain rtmp has no -tls_verify ----
    log = plan('ok')
    rec = wait_final(app.create_replay({**body, 'server': 'rtmp://10.0.0.1/live', 'loop': False})['id'])
    assert rec['status'] == 'finished', rec
    assert '-tls_verify' not in calls(log)[0]['argv'] and '-stream_loop' not in calls(log)[0]['argv']
    log = plan('short,fail')                   # exit 0 halfway: not "finished", it resumes where it stopped
    rec = wait_final(app.create_replay({**body, 'loop': False})['id'])
    c = calls(log)
    assert rec['status'] == 'failed' and len(c) == 4, (rec, len(c))
    assert files(c[1]['list'])[0] == 'seg_00000002.ts', f'resumed from {files(c[1]["list"])}'

    # ---- a one-pass that drops near the end resumes there, never from the start of the round ----
    log = plan('run0.6,ok', speed=13)        # ~8 s of the 10 s round goes out, then the link drops
    rec = wait_final(app.create_replay({**body, 'loop': False})['id'])
    c = calls(log)
    assert rec['status'] == 'finished' and len(c) == 2, rec
    assert files(c[1]['list'])[0] in ('seg_00000003.ts', 'seg_00000004.ts'), f'resumed from {files(c[1]["list"])}'
    log = plan('run0.6,ok', speed=40)        # the whole round went out before the drop was noticed
    wait_final(app.create_replay({**body, 'loop': False})['id'])
    assert files(calls(log)[1]['list']) == ['seg_00000004.ts'], 'at worst the last segment again, not the round'

    # ---- on air, then drops: retries; three quick drops in a row -> failed ----
    log = plan('run1,fail,fail,fail,fail')
    rec = wait_final(app.create_replay({**body, 'loop': True})['id'])
    assert rec['status'] == 'failed' and len(calls(log)) == 4, (rec, len(calls(log)))
    assert rec['streamed'] >= 1, rec

    # ---- stop button; limits while one is on air ----
    plan('hang')
    t0 = app.read_segments(ended)[0].start
    rid = app.create_replay({**body, 'loop': True, 't_in': t0 + 2, 't_out': t0 + 6})['id']
    wait_status(rid, 'streaming')
    assert raises(ClipError, app.create_replay, {**body, 'loop': True}), 'same server + key twice'
    app.REPLAY_MAX = 1
    assert raises(ClipError, app.create_replay, {**body, 'key': 'other-key'}), 'over REPLAY_MAX'
    app.REPLAY_MAX = 2
    app.stop_replay(rid)
    rec = wait_final(rid)
    assert rec['status'] == 'stopped' and rec['reason'] and not (app.REPLAYS / rid).exists(), rec
    assert raises(BadRequest, app.stop_replay, rid), 'stopping twice says so'

    # ---- a running replay stays listed (its row holds the only stop button) behind 20 newer ones ----
    plan('hang')
    old = app.create_replay({**body, 'key': 'k-old'})['id']
    wait_status(old, 'streaming')
    for i in range(21):
        app.save_replay({'id': f'{i:032x}', 'status': 'failed', 'created': time.time() + 1 + i})
    st = app.build_state()
    assert old in [r['id'] for r in st['replays']] and len(st['replays']) == 21, len(st['replays'])
    # ---- REPLAY_MAX_HOURS: a forgotten loop stops itself ----
    app.replay_watch_once(now=time.time() + app.REPLAY_MAX_HOURS * 3600 + 60)
    rec = wait_final(old)
    assert rec['status'] == 'stopped' and 'ชม.' in rec['reason'], rec
    for i in range(21):
        (app.REPLAYS / f'{i:032x}.json').unlink()

    # ---- the stage comes back on air: stopped by the watcher, unless asked not to ----
    plan('hang')
    keep = app.create_replay({**body, 'key': 'k-keep', 'stop_when_live': False})['id']
    stop = app.create_replay({**body, 'key': 'k-stop'})['id']
    wait_status(keep, 'streaming'); wait_status(stop, 'streaming')
    real = app.mediamtx_paths
    app.mediamtx_paths = lambda: None                      # API down: keep sending, never guess
    app.replay_watch_once()
    assert app.REPLAY_RUN.get(stop), 'unknown stage state stops nothing'
    app.mediamtx_paths = lambda: {'xstage': {'ready': True}}
    app.replay_watch_once()
    rec = wait_final(stop)
    assert rec['status'] == 'stopped' and 'กลับมาไลฟ์' in rec['reason'], rec
    assert app.REPLAY_RUN.get(keep), 'stop_when_live off: still sending'
    app.mediamtx_paths = real
    app.stop_replay(keep)
    wait_final(keep)

    # ---- restart: a record left "streaming" by a dead process becomes failed ----
    ghost = {'id': 'f' * 32, 'status': 'streaming', 'created': time.time(), 'stream': 'xstage'}
    app.save_replay(ghost)
    (app.REPLAYS / ghost['id']).mkdir()
    app.recover_replays()
    rec = json.loads((app.REPLAYS / f'{ghost["id"]}.json').read_text())
    assert rec['status'] == 'failed' and 'รีสตาร์ท' in rec['error'] and not (app.REPLAYS / ghost['id']).exists()

    # ---- a record left "streaming" by a thread that died (disk full) is swept to failed ----
    lost = {'id': 'd' * 32, 'status': 'streaming', 'created': time.time(), 'run_started': time.time()}
    app.save_replay(lost)
    os.utime(app.REPLAYS / f'{lost["id"]}.json', (time.time() - 300, time.time() - 300))
    app.janitor_once()
    assert json.loads((app.REPLAYS / f'{lost["id"]}.json').read_text())['status'] == 'failed'

    # ---- janitor drops day-old finished records, keeps recent ones ----
    old = {'id': 'e' * 32, 'status': 'finished', 'created': 0, 'ended': time.time() - 25 * 3600}
    app.save_replay(old)
    app.janitor_once()
    assert not (app.REPLAYS / f'{old["id"]}.json').exists()
    assert (app.REPLAYS / f'{ghost["id"]}.json').exists()

    # ---- the page gets the list, never a key ----
    app.mediamtx_paths = lambda: {}
    st = app.build_state()
    assert st['replays'] and KEY not in json.dumps(st) and 'k-keep' not in json.dumps(st)
    print('test_replay: all checks passed')


if __name__ == '__main__':
    main()
