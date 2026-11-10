"""Self-check for restream (push a LIVE stage to several platforms). Runs a fake ffmpeg and a fake
MediaMTX, so no network and no ffmpeg needed. Run: python test_restream.py"""
import json, os, stat, sys, tempfile, threading, time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ['DATA_DIR'] = str(tmp / 'data')
os.environ['MEDIAMTX_API_PASS'] = 'mtx-secret-pass'
import app  # noqa: E402  (reads DATA_DIR at import)
from app import BadRequest, ClipError, Forbidden  # noqa: E402

# One fake ffmpeg for every destination: what it does is looked up by the stream key it is given.
# Like the real one it prints a progress block twice a "second" WHETHER OR NOT anything moves.
FAKE = tmp / 'fake_ffmpeg.py'
FAKE.write_text(r'''
import json, os, signal, sys, time
key = sys.argv[sys.argv.index('-rtmp_playpath') + 1]
d = os.environ['FAKE_DIR']
plan = json.load(open(d + '/plan.json'))[key]
cnt = d + '/count-' + key
n = int(open(cnt).read()) if os.path.exists(cnt) else 0
open(cnt, 'w').write(str(n + 1))
step = plan[min(n, len(plan) - 1)]
with open(d + '/calls-' + key, 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
signal.signal(signal.SIGTERM, lambda *a: sys.exit(255))
src, url = sys.argv[sys.argv.index('-i') + 1], sys.argv[-1]
def block(t):
    print('bitrate= 512.3kbits/s', file=sys.stderr)          # real ffmpeg pads some values
    print('speed=   1x', file=sys.stderr)
    print(f'total_size={int(t * 700000)}', file=sys.stderr)
    print(f'out_time_us={int(t * 1e6)}', file=sys.stderr)
    print('progress=continue', file=sys.stderr, flush=True)
def on_air(seconds, frozen_after=None):
    t0 = time.time()
    while seconds is None or time.time() - t0 < seconds:
        t = max(0.05, time.time() - t0)
        block(min(t, frozen_after) if frozen_after else t)   # frozen: same numbers, lines keep coming
        time.sleep(0.05)
if step == 'refuse':                    # the platform refuses the key: output never opens
    print('[tls @ 0x1] IO error: End of file', file=sys.stderr)
    print(f'[out#0/flv @ 0x2] Error opening output {url}/{key} I/O error', file=sys.stderr)
    sys.exit(1)
if step == 'nosrc':                     # ffmpeg cannot open the stage (RTSP off, no read permission)
    print('[in#0 @ 0x3] Error opening input: Server returned 401 Unauthorized', file=sys.stderr)
    print(f'Error opening input file {src}.', file=sys.stderr)
    sys.exit(8)
if step.startswith('air'):              # sending, then the platform drops the connection
    on_air(float(step[3:]))
    print(f'[out#0/flv @ 0x2] Error writing trailer of {url}/{key}: Broken pipe', file=sys.stderr)
    sys.exit(1)
if step.startswith('end'):              # sending, then the input ends: ffmpeg finishes with 0
    on_air(float(step[3:]))
    sys.exit(0)
if step == 'freeze':                    # sending, then nothing moves but the process stays and reports
    on_air(None, frozen_after=0.15)
if step == 'lag':                       # bytes keep leaving, but only half a second of picture per second
    t0 = time.time()
    while True:
        t = time.time() - t0
        print(f'total_size={int(t * 700000) + 500}', file=sys.stderr)
        print(f'out_time_us={int((0.05 + t * 0.5) * 1e6)}', file=sys.stderr)
        print('progress=continue', file=sys.stderr, flush=True)
        time.sleep(0.05)
if step == 'deaf':                      # sending, and slow to close when told to stop
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    on_air(None)
if step == 'slow':                      # never gets through to the platform, never says so
    while True:
        time.sleep(0.05)
on_air(None)                            # 'hang': sending until told to stop
''')
app.REPLAY_FFMPEG = [sys.executable, str(FAKE)]
app.REPLAY_STEADY = 0.4
app.RESTREAM_PIN = 'test-pin-abc123'
app.RESTREAM_TICK = 0.05
app.RESTREAM_SAMPLE_GAP = 0.05
app.RESTREAM_MIN_GAP = 0.01
app.RESTREAM_FRESH = 0.2
app.RESTREAM_STALL = 0.5
app.RESTREAM_CONNECT = 0.6
app.RESTREAM_GIVEUP = 1.2
app.RESTREAM_LAG_WINDOW = 0.9                # longer than RESTREAM_STALL, as in production (60 s vs 20 s)
app.RESTREAM_LAG_MAX = 0.2
app.RESTREAM_WARN_AFTER = 0.3
app.RESTREAM_BACKOFF = (0.05, 0.05)
app.RECONNECT_MIN = 0.02                    # the "stage is away" window: 1.2 s here
app.host_addrs = lambda host: {'142.250.66.108'}   # a public address; no DNS in the test
for d in (app.DVR, app.CLIPS, app.SETTINGS, app.REPLAYS, app.RESTREAM, app.YOUTUBE):
    d.mkdir(parents=True, exist_ok=True)
os.environ['FAKE_DIR'] = str(tmp)

# Fake MediaMTX: a stage is listed while "ready"; its byte counter moves only while it delivers.
READY, DELIVERING, API_UP, BYTES, TRACKS = set(), set(), [True], {}, {}


def fake_paths():
    if not API_UP[0]:
        return None
    for name in DELIVERING:
        BYTES[name] = BYTES.get(name, 0) + 1000
    return {name: {'ready': True, 'bytesReceived': BYTES.get(name, 0),
                   'tracks': TRACKS.get(name, ['H264', 'MPEG-4 Audio'])} for name in READY}


app.mediamtx_paths = fake_paths
threading.Thread(target=app.restream_watch_loop, daemon=True).start()
YT = 'rtmps://a.rtmps.youtube.com:443/live2'
TK = 'k-tk?expire=1793900000&sign=abc'


def stage_on(name, delivering=True):
    READY.add(name)
    (DELIVERING.add if delivering else DELIVERING.discard)(name)


def stage_off(name):
    READY.discard(name)
    DELIVERING.discard(name)


def plan(steps):
    for f in list(tmp.glob('count-*')) + list(tmp.glob('calls-*')):
        f.unlink()
    (tmp / 'plan.json').write_text(json.dumps(steps))


def calls(key):
    f = tmp / f'calls-{key}'
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def add(stage, name, key, server=YT):
    return app.save_restream_dest(stage, {'name': name, 'server': server, 'key': key})['id']


def view(stage, did):
    return next(d for d in app.restream_state([stage])['stages'][stage]['dests'] if d['id'] == did)


def wait(stage, did, status, timeout=8):
    end, seen = time.time() + timeout, []
    while time.time() < end:
        v = view(stage, did)
        if not seen or seen[-1] != v['status']:
            seen.append(v['status'])
        if v['status'] == status:
            return v
        time.sleep(0.01)
    raise AssertionError(f'{did[:6]} never reached {status}: saw {seen}, now {view(stage, did)}')


def gone(stage, did, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        if (stage, did) not in app.RESTREAM_RUN:
            return view(stage, did)
        time.sleep(0.01)
    raise AssertionError(f'push thread still alive: {view(stage, did)}')


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    return False


def started(stage, ids):
    res = app.start_restream(stage, ids)
    assert all(r['ok'] for r in res), res


def stop_all():
    for (stage, did) in list(app.RESTREAM_RUN):
        app.stop_restream(stage, [did])
    for (stage, did) in list(app.RESTREAM_RUN):
        gone(stage, did)


def main():
    # ---- the second secret: off without it; wrong is refused and made loud, but never locks Stop out ----
    assert raises(Forbidden, app.check_pin, {}) and raises(Forbidden, app.check_pin, {'pin': 12345})
    app.check_pin({'pin': 'test-pin-abc123'})
    for _ in range(5):
        raises(Forbidden, app.check_pin, {'pin': 'guess'})
    app.check_pin({'pin': 'test-pin-abc123'})   # the right PIN still works: a lockout would let anyone block Stop
    assert any('PIN' in w for w in app.WARNINGS.values()), 'guessing shows on every editor\'s page'
    real_pin, app.RESTREAM_PIN = app.RESTREAM_PIN, ''
    assert raises(Forbidden, app.check_pin, {'pin': ''}), 'no RESTREAM_PIN on the server = feature off (empty never matches)'
    assert app.restream_state([])['enabled'] is False
    app.RESTREAM_PIN = real_pin

    # ---- saving destinations: checked, 4 per stage, key never comes back, file is 0600 ----
    assert raises(BadRequest, add, 'main', '', 'k') and raises(BadRequest, add, 'main', 'x', '')
    assert raises(BadRequest, add, 'main', 'x', 'k', 'http://evil/') and raises(BadRequest, add, 'bad stage!', 'x', 'k')
    assert raises(BadRequest, add, 'main', 'x', 'k\x00y'), 'control characters would crash ffmpeg at start'
    yt = add('main', 'YouTube public', 'k-yt-0123456789')
    fb = add('main', 'Facebook', 'k-fb', 'rtmps://live-api-s.facebook.com:443/rtmp/')
    tk = add('main', 'TikTok', TK, 'rtmps://push.example.com:443/game/')
    yp = add('main', 'YouTube private', 'k-yp')
    assert raises(ClipError, add, 'main', 'fifth', 'k5'), 'four per stage'
    f = app.RESTREAM / 'main.json'
    assert stat.S_IMODE(f.stat().st_mode) == 0o600, 'the file holds stream keys'
    st = json.dumps(app.restream_state(['main']))
    assert 'k-yt-0123' not in st and 'sign=abc' not in st and 'mtx-secret-pass' not in st, 'no key in what the page gets'
    v = view('main', yt)
    assert v['key_tail'] == '6789' and v['key_saved_at'] and v['has_key'] and v['status'] == 'off', v
    assert view('main', tk)['key_expires'] == 1793900000 and view('main', fb)['key_tail'] == '', 'a short key shows no tail'
    app.save_restream_dest('main', {'id': yt, 'name': 'YT public', 'server': YT, 'key': ''})
    assert json.loads(f.read_text())[0]['key'] == 'k-yt-0123456789', 'an empty key on edit keeps the saved one'
    assert raises(BadRequest, app.save_restream_dest, 'main', {'id': yt, 'name': 'YT public', 'server': 'rtmp://their-host.example/live', 'key': ''}), \
        'pointing a saved key at another server would hand the key to that server'
    assert json.loads(f.read_text())[0]['server'] == YT
    KY = 'k-yt-0123456789'
    assert app.rtmp_dest('rtmps://A.RTMPS.YouTube.com./live2', KY)['ident'] == app.rtmp_dest('rtmps://a.rtmps.youtube.com:443/live2/', KY)['ident'], \
        'the same server typed another way is still the same server + key'

    # ---- start needs a stage that is really delivering, with picture and sound, and no old hook armed ----
    plan({KY: ['hang'], 'k-fb': ['hang'], 'k-yp': ['hang'], TK: ['hang']})
    r = raises(ClipError, app.start_restream, 'main', [yt])
    assert r, 'stage offline'
    stage_on('main', delivering=False)
    assert raises(ClipError, app.start_restream, 'main', [yt]), 'encoder connected but sending nothing (4 Nov 2026)'
    API_UP[0] = False
    try:
        app.start_restream('main', [yt]); raise AssertionError('unknown is not "live"')
    except ClipError as e:
        assert 'MediaMTX' in str(e) and 'ไม่มีสัญญาณ' not in str(e), f'cannot ask is not "no signal": {e}'
    API_UP[0] = True
    stage_on('main')
    TRACKS['main'] = ['H264']
    assert raises(ClipError, app.start_restream, 'main', [yt]), 'video without sound'
    del TRACKS['main']
    (app.YOUTUBE / 'main.key').write_text('x')
    assert raises(ClipError, app.start_restream, 'main', [yt]), 'the old YouTube hook is armed for this stage'
    (app.YOUTUBE / 'main.key').unlink()
    assert not app.RESTREAM_RUN and not calls(KY), 'a refused start launches nothing'

    # ---- a normal show: "sending" only on real output; reads the stage over RTSP; key never in the URL ----
    started('main', [yt])
    wait('main', yt, 'streaming')
    time.sleep(0.3)
    v = view('main', yt)
    assert v['status'] == 'streaming' and v['armed'] and v['air'] > 0.2 and v['idle'] < 0.2, v
    argv = calls(KY)[0]
    assert argv[argv.index('-i') + 1] == 'srt://mediamtx:8890?streamid=read:main:liveclip:mtx-secret-pass', argv
    assert '-rtsp_transport' not in argv and argv[argv.index('-rw_timeout') + 1] == '10000000', 'SRT keeps the encoder\'s A/V timing'
    assert argv[argv.index('-c') + 1] == 'copy' and argv[-1] == YT and argv[argv.index('-rtmp_playpath') + 1] == KY
    assert argv[argv.index('-tls_verify') + 1] == '1' and '-re' not in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == '-map'] == ['0:v:0', '0:a:0?'], 'one video + one audio track'
    assert raises(ClipError, app.save_restream_dest, 'main', {'id': yt, 'name': 'x', 'server': YT, 'key': 'k2'}), 'no edit while sending'
    assert raises(ClipError, app.delete_restream_dest, 'main', yt), 'no delete while sending'
    again = app.start_restream('main', [yt])[0]
    assert again['ok'] and again['note'] == 'ส่งอยู่แล้ว' and len(calls(KY)) == 1, 'Start on a row that is sending is not an error and starts nothing'
    # an ended round of this stage exists: a replay of it must wait while the live show holds the uplink
    old = app.DVR / 'main' / '20261104-060000'
    old.mkdir(parents=True)
    (old / 'seg_00000000.ts').write_bytes(b'ts')
    (old / 'index.m3u8').write_text('#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-11-04T07:00:00.000+0000\n#EXTINF:2.000000,\nseg_00000000.ts\n')
    os.utime(old / 'index.m3u8', (time.time() - 3600, time.time() - 3600))
    try:
        app.create_replay({'stream': 'main', 'session': old.name, 'server': YT, 'key': 'another-key', 'stop_when_live': False})
        raise AssertionError('a replay started while a restream holds the uplink')
    except ClipError as e:
        assert 'Restream' in str(e), e
    assert not app.REPLAY_RUN
    assert app.stop_restream('main', [yt])[0]['ok']
    assert json.loads(f.read_text())[0]['armed'] is False, 'Stop is on disk before ffmpeg has even gone'
    v = gone('main', yt)
    assert v['status'] == 'stopped' and v['by'] == 'user' and not v['armed'] and v['air'] > 0.2 and 'กดหยุด' in v['text'], v
    assert 'แพลตฟอร์ม' in v['text'], 'a stop here does not end the broadcast on the platform, and says so'
    assert app.stop_restream('main', [yt])[0]['ok'] is False, 'stopping twice says so'

    # ---- wrong key: two refusals and it is failed, loudly, without the key or the MediaMTX login ----
    plan({KY: ['refuse']})
    started('main', [yt])
    v = gone('main', yt)
    assert v['status'] == 'failed' and 'ตรวจ Server' in v['text'] and 'End of file' in v['text'], v
    assert KY not in v['text'] and 'rtmp' not in v['text'] and 'mtx-secret' not in v['text'], v
    assert 'speed' not in v['text'] and 'bitrate' not in v['text'], 'progress lines are not error text'
    assert len(calls(KY)) == 2 and not v['armed'], 'a refused key is tried twice, then nothing resurrects it'
    assert any('YT public' in w and 'ล้มเหลว' in w for w in app.WARNINGS.values()), 'a failure shows in the bar on every page'

    # ---- the stage cannot be READ (RTSP off / no permission): our side, never "check the key" ----
    plan({KY: ['nosrc']})
    started('main', [yt])
    v = gone('main', yt)
    assert v['status'] == 'failed' and 'MediaMTX' in v['text'] and 'ตรวจ Server กับ Stream key' not in v['text'], v
    assert 'mtx-secret' not in v['text'] and 'srt://' not in v['text'], f'the MediaMTX login must not reach the page: {v["text"]}'

    # ---- one platform drops mid-show: only that one retries, the others never notice ----
    plan({KY: ['air0.7', 'refuse', 'hang'], 'k-fb': ['hang']})
    started('main', [yt, fb])
    wait('main', fb, 'streaming')
    wait('main', yt, 'retrying')
    v = wait('main', yt, 'streaming')
    assert v['retries'] >= 1 and len(calls('k-fb')) == 1 and view('main', fb)['retries'] == 0, (v, len(calls('k-fb')))
    stop_all()

    # ---- a platform that takes the stream and drops it within seconds, again and again: failed, not a flap ----
    plan({KY: ['air0.1']})
    started('main', [yt])
    v = gone('main', yt)
    assert v['status'] == 'failed' and len(calls(KY)) == 2, f'short spells are not "it works": {v}, {len(calls(KY))} tries'

    # ---- a platform that stays dead after having worked: retried for RESTREAM_GIVEUP, then failed ----
    plan({KY: ['air0.7', 'air0.1']})
    started('main', [yt])
    wait('main', yt, 'retrying')
    time.sleep(0.6)
    app.restream_state(['main'])
    assert any('ส่งไม่นิ่ง' in w for w in app.WARNINGS.values()), f'a channel that flaps must reach the yellow bar: {app.WARNINGS}'
    v = gone('main', yt, timeout=8)
    assert v['status'] == 'failed' and 'key นี้' in v['text'] and len(calls(KY)) > 3, (v, len(calls(KY)))

    # ---- ffmpeg ends by itself while the stage is fine (MediaMTX dropped this reader): a counted retry ----
    plan({KY: ['end0.7', 'hang']})
    started('main', [yt])
    v = wait('main', yt, 'retrying')
    assert 'การอ่านสัญญาณเวทีหลุด' in v['text'] and 'ไม่มีสัญญาณเข้า' not in v['text'], f'the stage is fine, do not blame it: {v}'
    wait('main', yt, 'streaming')
    stop_all()

    # ---- MediaMTX keeps dropping the SRT read (messy encoder, 4 Nov 2026): switch to RTSP and say what it costs ----
    plan({KY: ['end0.5', 'end0.1', 'end0.1', 'end0.1', 'hang']})
    started('main', [yt])
    wait('main', yt, 'streaming')            # first spell is a good one, so the key counts as proven
    deadline = time.time() + 8
    while time.time() < deadline and not any('rtsp://' in a[a.index('-i') + 1] for a in calls(KY)):
        time.sleep(0.02)
    v = wait('main', yt, 'streaming')
    srcs = [a[a.index('-i') + 1].split(':')[0] for a in calls(KY)]
    assert srcs[:3] == ['srt', 'srt', 'srt'] and srcs[-1] == 'rtsp', srcs
    last = calls(KY)[-1]
    assert last[last.index('-i') + 1] == 'rtsp://liveclip:mtx-secret-pass@mediamtx:8554/main' and '-rtsp_transport' in last
    assert v['note'] and 'RTSP' in v['note'] and 'เสียง' in v['note'], f'the price of the fallback is on the row: {v}'
    stop_all()

    # ---- bytes leave but the push falls behind real time (uplink too small): cut and reconnect, with the reason ----
    plan({KY: ['lag', 'hang']})
    started('main', [yt])
    v = wait('main', yt, 'retrying')
    assert 'รับไม่ทันเวลาจริง' in v['text'], v
    wait('main', yt, 'streaming')
    stop_all()

    # ---- a channel that worked, then the stage blips, then the platform refuses twice: still "it has worked" ----
    plan({KY: ['end0.7', 'refuse', 'refuse', 'hang']})
    started('main', [yt])
    wait('main', yt, 'streaming')
    time.sleep(0.5)                           # a good spell, still running
    assert json.loads(f.read_text())[0]['proven'] is True, 'credited while it runs, not when the attempt ends'
    stage_off('main')
    wait('main', yt, 'waiting')
    stage_on('main')
    v = wait('main', yt, 'streaming', timeout=10)
    assert len(calls(KY)) == 4, f'two refusals after an hour of sending are retried, not "check the key": {len(calls(KY))}'
    stop_all()

    # ---- MediaMTX drops the SRT read from the very first seconds: the fallback must still be reached ----
    plan({KY: ['end0.1', 'end0.1', 'end0.1', 'hang']})
    started('main', [yt])
    deadline = time.time() + 10
    while time.time() < deadline and len(calls(KY)) < 4:
        time.sleep(0.02)
    v = wait('main', yt, 'streaming', timeout=10)
    srcs = [a[a.index('-i') + 1].split(':')[0] for a in calls(KY)]
    assert srcs == ['srt', 'srt', 'srt', 'rtsp'] and v['note'] and 'RTSP' in v['note'], (srcs, v)
    stop_all()

    # ---- Start while the channel is still closing: refused with the reason, never "already sending" ----
    plan({KY: ['deaf']})
    started('main', [yt])
    wait('main', yt, 'streaming')
    app.stop_restream('main', [yt])
    assert view('main', yt)['status'] == 'stopping'
    r = app.start_restream('main', [yt])[0]
    assert r['ok'] is False and 'กำลังหยุด' in r['error'], r
    app.RESTREAM_RUN[('main', yt)]['proc'].kill()
    gone('main', yt)
    assert view('main', yt)['status'] == 'stopped', 'the refusal did not overwrite how it really ended'

    # ---- the stage drops: every channel waits (no retries against the platform) and resumes by itself ----
    plan({KY: ['end0.6', 'end0.8', 'hang']})   # real ffmpeg ends cleanly when MediaMTX closes the reader
    started('main', [yt])
    wait('main', yt, 'streaming')
    stage_off('main')
    v = wait('main', yt, 'waiting')
    assert 'เวทีไม่มีสัญญาณ' in v['text'] and v['armed'], v
    time.sleep(0.3)
    n = len(calls(KY))
    assert n <= 2 and view('main', yt)['status'] == 'waiting', 'nothing is pushed while the stage is away'
    stage_on('main')
    v = wait('main', yt, 'streaming')
    assert len(calls(KY)) == n + 1 and v['retries'] == 0, 'the stage coming back is not a platform retry'

    # ---- MediaMTX cannot be asked while the stage ended: "cannot tell", never "no signal" ----
    API_UP[0] = False
    v = wait('main', yt, 'waiting')
    assert 'ติดต่อ MediaMTX ไม่ได้' in v['text'] and 'ไม่มีสัญญาณเข้า' not in v['text'], v
    API_UP[0] = True

    # ---- the stage stays away past the reconnect window: stopped BY THE SYSTEM, disarmed, in the bar ----
    stage_off('main')
    v = gone('main', yt, timeout=8)
    assert v['status'] == 'stopped' and v['by'] == 'system' and not v['armed'] and 'หยุดส่ง' in v['text'], v
    assert any('ระบบหยุดส่งเอง' in w for w in app.WARNINGS.values()), 'an automatic stop is not painted like a person\'s Stop'

    # ---- encoder holds the line but sends nothing: ffmpeg keeps REPORTING with frozen numbers ----
    stage_on('main')
    plan({KY: ['freeze', 'hang']})
    started('main', [yt])
    wait('main', yt, 'streaming')
    DELIVERING.discard('main')                # still "ready", byte counter frozen
    v = wait('main', yt, 'stalled', timeout=3)
    assert 'ไม่มีข้อมูลออก' in v['text'] and v['kbps'] is None, f'a frozen push must not read as sending: {v}'
    v = wait('main', yt, 'waiting')
    assert 'ไม่มีภาพเข้า' in v['text'], v
    assert len(calls(KY)) == 1, 'no reconnect to the platform while the stage sends nothing'
    DELIVERING.add('main')
    wait('main', yt, 'streaming')
    assert len(calls(KY)) == 2

    # ---- ffmpeg freezes while the stage is fine: killed and retried, never left saying "sending" ----
    plan({KY: ['hang'], 'k-fb': ['freeze', 'hang']})
    started('main', [fb])
    wait('main', fb, 'streaming')
    v = wait('main', fb, 'retrying')
    assert 'ไม่มีข้อมูลออก' in v['text'], v
    wait('main', fb, 'streaming')
    stop_all()

    # ---- a platform that never answers: killed after RESTREAM_CONNECT, twice, then failed ----
    plan({'k-fb': ['slow']})
    started('main', [fb])
    v = gone('main', fb, timeout=8)
    assert v['status'] == 'failed' and 'ภาพแรกไม่ออก' in v['text'] and len(calls('k-fb')) == 2, v

    # ---- limits: RESTREAM_MAX at once incl. replays; one server+key = one push; replay waits for the show ----
    plan({KY: ['hang'], 'k-fb': ['hang'], 'k-yp': ['hang'], 'k-x': ['hang']})
    app.RESTREAM_MAX = 2
    res = app.start_restream('main', [yt, fb, yp])
    assert [r['ok'] for r in res] == [True, True, False] and 'ไม่เกิน 2' in res[2]['error'], res
    v = view('main', yp)
    assert v['status'] == 'failed' and 'ไม่ได้เริ่มส่ง' in v['text'], f'a refusal stays on the row after the next poll: {v}'
    app.RESTREAM_MAX = 4
    stage_on('xstage')
    dup = add('xstage', 'same key again', KY)
    res = app.start_restream('xstage', [dup])
    assert not res[0]['ok'] and 'กำลังถูกใช้' in res[0]['error'], res
    app.REPLAY_RUN['r1'] = {'dest': app.rtmp_dest(YT, 'k-x')}
    other = add('xstage', 'used by a replay', 'k-x')
    assert not app.start_restream('xstage', [other])[0]['ok'], 'a replay already sends to this key'
    del app.REPLAY_RUN['r1']
    wait('main', yt, 'streaming'); wait('main', fb, 'streaming')

    # ---- MediaMTX API goes away mid-show: nothing is stopped, nothing new is started ----
    API_UP[0] = False
    time.sleep(0.4)
    assert view('main', yt)['status'] == 'streaming' and view('main', fb)['status'] == 'streaming'
    API_UP[0] = True

    # ---- app restart: what was being sent carries on, unless the app was away longer than the window ----
    def restart(away_s, pin=True):
        for run in list(app.RESTREAM_RUN.values()):      # the old process dies with its ffmpegs
            run['stop'].set()
            if run['proc']:
                run['proc'].kill()
        deadline = time.time() + 5
        while time.time() < deadline:
            with app.RESTREAM_LOCK:                      # a thread's last write happens under this lock
                if not app.RESTREAM_RUN:
                    break
            time.sleep(0.01)
        with app.RESTREAM_LOCK:
            dests = json.loads(f.read_text())
            for d in dests:
                d['armed'] = d['id'] in (yt, fb)         # as a killed process leaves the file
            app.save_dests('main', dests)
        alive = app.RESTREAM / '.alive'
        alive.touch()
        os.utime(alive, (time.time() - away_s, time.time() - away_s))
        app.RESTREAM_PIN = real_pin if pin else ''
        app.resume_restreams()
        app.RESTREAM_PIN = real_pin
    plan({KY: ['hang'], 'k-fb': ['hang']})
    restart(away_s=0.2)
    wait('main', yt, 'streaming'); wait('main', fb, 'streaming')
    restart(away_s=60)
    v = view('main', yt)
    assert not app.RESTREAM_RUN and v['status'] == 'stopped' and v['by'] == 'system' and not v['armed'] and 'ไม่ส่งต่อให้เอง' in v['text'], v
    restart(away_s=0.2, pin=False)
    assert not app.RESTREAM_RUN and not view('main', yt)['armed'], 'no PIN on the server: nothing resumes'

    # ---- a restart mid-show keeps "this key has worked": two quick refusals do not fail the channel ----
    plan({KY: ['refuse', 'refuse', 'refuse', 'hang']})
    dests = json.loads(f.read_text())
    dests[0].update(armed=True, proven=True)
    app.save_dests('main', dests)
    (app.RESTREAM / '.alive').touch()
    app.resume_restreams()
    wait('main', yt, 'streaming')
    assert len(calls(KY)) == 4
    stop_all()

    # ---- a destination left "armed" with no thread (its end could not be written) reads as failed ----
    dests = json.loads(f.read_text())
    dests[0]['armed'] = True
    app.save_dests('main', dests)
    assert view('main', yt)['status'] == 'failed' and 'ไม่ได้บันทึกสาเหตุ' in view('main', yt)['text']
    app.repair_restreams()
    d0 = json.loads(f.read_text())[0]
    assert d0['armed'] is False and d0['last']['status'] == 'failed', 'the janitor writes the missing end, so a restart cannot push it again'

    # ---- the event log answers "what happened this afternoon" after the rows have moved on ----
    log = app.restream_state(['main'])['stages']['main']['log']
    texts = ' | '.join(e['text'] for e in log)
    assert 'กดเริ่มส่ง' in texts and 'ล้มเหลว' in texts and 'หยุดส่งแล้ว' in texts and all(e['at'] and e['dest'] for e in log), texts
    assert KY not in (app.RESTREAM / 'main.log').read_text() and 'mtx-secret' not in (app.RESTREAM / 'main.log').read_text()

    # ---- delete; a corrupt file is a warning on the page, not "no destinations" ----
    app.delete_restream_dest('main', yp)
    assert len(app.restream_state(['main'])['stages']['main']['dests']) == 3 and raises(BadRequest, app.delete_restream_dest, 'main', yp)
    (app.RESTREAM / 'broken.json').write_text('{not json')
    st = app.restream_state(['main'])
    assert st['stages']['broken']['error'] and st['stages']['broken']['dests'] == [], 'said on the card, not shown as an empty stage'
    assert any('broken' in w for w in app.WARNINGS.values()), app.WARNINGS
    (app.RESTREAM / 'Not A Stage.json').write_text('[]')
    app.resume_restreams()                    # a broken or stray file must not stop the app from starting
    (app.RESTREAM / 'broken.json').unlink(); (app.RESTREAM / 'Not A Stage.json').unlink()

    # ---- what the page gets never holds a key, the MediaMTX login or the PIN, even after all of the above ----
    st = json.dumps(app.build_state())
    for secret in (KY, 'k-fb', 'sign=abc', 'mtx-secret-pass', 'test-pin-abc123'):
        assert secret not in st, secret
    print('test_restream: all checks passed')


if __name__ == '__main__':
    main()
