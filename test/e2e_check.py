"""End-to-end check, run INSIDE the web container while a test stream is live:
    docker compose -p liveclip exec -T web python /app/e2e_check.py
Creates clips through the HTTP API and checks by picture (PSNR) that a clip starts and ends
on the requested frames, then that the stored object matches byte for byte. Exits 1 on any miss.
"""
import base64, json, os, pathlib, subprocess, sys, time, urllib.request

import app

AUTH = 'Basic ' + base64.b64encode(f"{os.environ['WEB_USER']}:{os.environ['WEB_PASSWORD']}".encode()).decode()
BASE = 'http://127.0.0.1:8080'
TMP = pathlib.Path('/data/e2e'); TMP.mkdir(exist_ok=True)
fails = []


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': AUTH, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def frame(src, out, ss=None, last=False):
    cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(src)]
    if last:
        cmd = ['ffmpeg', '-v', 'error', '-y', '-sseof', '-0.2', '-i', str(src), '-update', '1']
    if ss is not None:
        cmd += ['-ss', f'{ss:.3f}']
    subprocess.run(cmd + ['-frames:v', '1' if not last else '100', str(out)], check=True)
    return out


def psnr(a, b):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(a), '-i', str(b), '-lavfi', 'psnr', '-f', 'null', '-'],
                       capture_output=True, text=True)
    line = [l for l in r.stderr.splitlines() if 'average:' in l][-1]
    v = line.split('average:')[1].split()[0]
    return 99.0 if v == 'inf' else float(v)


def check(name, ok, detail):
    print(('PASS ' if ok else 'FAIL ') + name + ' ' + detail)
    if not ok:
        fails.append(name)


def concat_ref(segs, t, out):
    """Reference frame at concat time t, decoded from the raw segments (output-side seek = exact)."""
    lst = TMP / 'ref.ffconcat'
    lst.write_text('ffconcat version 1.0\n' + ''.join(f"file '{d / s.name}'\nduration {s.dur:.6f}\n" for s in segs))
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-safe', '0', '-f', 'concat', '-i', str(lst), '-ss', f'{t:.3f}',
                    '-frames:v', '1', str(out)], check=True)
    return out


state = call('/api/state')
live = [(s['name'], x) for s in state['streams'] for x in s['sessions'] if x['live'] and s['name'] == os.environ.get('E2E_STREAM', s['name'])]
if not live:
    sys.exit('no live session: start the test publisher first')
stream, sess = live[0]
d = pathlib.Path('/data/dvr') / stream / sess['id']
segs = app.read_segments(d)
# a 7.3 s range that starts mid-segment, ending 4 s behind the live edge
t_out = segs[-1].end - 4
t_in = t_out - 7.3
ids = {}
for mode in ('fast', 'accurate'):
    c = call('/api/clips', {'stream': stream, 'session': sess['id'], 't_in': t_in, 't_out': t_out,
                            'name': f'e2e {mode}', 'accurate': mode == 'accurate'})
    ids[mode] = c['id']
deadline = time.time() + 120
while time.time() < deadline:
    clips = {c['id']: c for c in call('/api/clips')}
    if all(clips[i]['status'] in ('done', 'failed') for i in ids.values()):
        break
    time.sleep(1)
for mode, cid in ids.items():
    c = clips[cid]
    check(f'{mode}: status', c['status'] == 'done', f"{c['status']} {c.get('error') or ''}")

chosen, offset, dur, gap = app.plan_cut(segs, t_in, t_out)
if not fails:
    acc, fast = (pathlib.Path(f'/data/clips/{ids[m]}.mp4') for m in ('accurate', 'fast'))
    fps = 30.0
    p = psnr(frame(acc, TMP / 'acc_first.png'), concat_ref(chosen, offset, TMP / 'ref_in.png'))
    check('accurate: first frame == In', p > 35, f'PSNR {p:.1f} dB')
    p = psnr(frame(acc, TMP / 'acc_last.png', last=True), concat_ref(chosen, offset + dur - 1 / fps, TMP / 'ref_out.png'))
    check('accurate: last frame == Out', p > 35, f'PSNR {p:.1f} dB')
    p = psnr(frame(fast, TMP / 'fast_first.png'), concat_ref(chosen, 0, TMP / 'ref_seg0.png'))
    check('fast: first frame == first keyframe at/before In', p > 35, f'PSNR {p:.1f} dB (preroll {clips[ids["fast"]]["preroll"]} s)')
    check('accurate: duration', abs(clips[ids['accurate']]['duration'] - 7.3) < 0.1, str(clips[ids['accurate']]['duration']))
    if app.S3_ON:
        s3 = app.s3_client()
        for mode, cid in ids.items():
            c = clips[cid]
            data = urllib.request.urlopen(app.presign(c)).read()
            local = pathlib.Path(f'/data/clips/{cid}.mp4').read_bytes()
            check(f'{mode}: presigned download == local file', data == local, f'{len(data)} bytes')
print('e2e:', 'ALL PASSED' if not fails else f'{len(fails)} FAILED: {fails}')
sys.exit(1 if fails else 0)
