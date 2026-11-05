"""live-clip: DVR buffer + clip cutter for SRT live streams.

MediaMTX receives SRT; its runOnReady hook (dvr.sh) writes a rolling HLS buffer per
stream under DATA_DIR/dvr/<stream>/<session>/. This app serves that buffer to the
browser, cuts clips with ffmpeg, and uploads them to S3-compatible storage (Wasabi).
Stdlib HTTP server on purpose: a handful of editors, one process. See README.md.
"""
import base64
import gzip
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

log = logging.getLogger('live-clip')

DATA = Path(os.environ.get('DATA_DIR', '/data'))
DVR, CLIPS, SETTINGS = DATA / 'dvr', DATA / 'clips', DATA / 'settings'
YOUTUBE = DATA / 'youtube'   # cloud/rtmp/youtube.sh: <stage>.key arms the push, <stage>.status reports it
HERE = Path(__file__).resolve().parent
WEB_USER = os.environ.get('WEB_USER', '')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '')
S3 = {k: os.environ.get('S3_' + k, '').strip() for k in
      ('ENDPOINT', 'REGION', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY', 'PREFIX')}
S3_REQUIRED = ('ENDPOINT', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY')
S3_ON = all(S3[k] for k in S3_REQUIRED)
MEDIAMTX_API = os.environ.get('MEDIAMTX_API', 'http://mediamtx:9997')
SRT_PUBLIC_HOST = os.environ.get('SRT_PUBLIC_HOST', '')
SRT_PUBLIC_PORT = os.environ.get('SRT_PUBLIC_PORT', '8890')
INGEST_HELP = os.environ.get('INGEST_HELP', 'srt')  # 'rtmp': per-stage RTMP keys (docker-compose.rtmp.yml)
MIN_FREE_GB = float(os.environ.get('MIN_FREE_GB', '20'))
LOCAL_CLIP_HOURS = float(os.environ.get('LOCAL_CLIP_HOURS', '24'))
CUT_WORKERS = int(os.environ.get('CUT_WORKERS', '2'))
RECONNECT_MIN = int(os.environ.get('RECONNECT_MIN', '10'))  # same value dvr.sh uses
MEDIAMTX_API_USER = os.environ.get('MEDIAMTX_API_USER', 'liveclip')
MEDIAMTX_API_PASS = os.environ.get('MEDIAMTX_API_PASS', '')
BKK = timezone(timedelta(hours=7))  # Thailand has no DST
LIVE_SECONDS = 20          # playlist rewritten within this many seconds = stream is live
ORPHAN_SECONDS = 120       # segment files older than this and not in the playlist get removed

# Shared with dvr.sh, which reads DATA_DIR/settings/buffer_minutes as a plain integer.
DEFAULTS = {'buffer_minutes': 120, 'keep_hours': 24}
LIMITS = {'buffer_minutes': (1, 720), 'keep_hours': (1, 168)}

NAME_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
SESSION_RE = re.compile(r'^\d{8}-\d{6}$')
SEG_RE = re.compile(r'^seg_\d{1,12}\.ts$')
CLIP_ID_RE = re.compile(r'^[0-9a-f]{32}$')
PDT_OFFSET_RE = re.compile(r'^(#EXT-X-PROGRAM-DATE-TIME:.*[+-]\d{2})(\d{2})$', re.M)

LOCK = threading.Lock()                 # guards clip json writes + WARNINGS
WARNINGS = {}                           # source -> message, shown in the UI
CUT_POOL = ThreadPoolExecutor(max_workers=CUT_WORKERS)
UPLOAD_POOL = ThreadPoolExecutor(max_workers=2)
RUNNING = set()                         # (clip id, stage) submitted and not finished; LOCK
EARLY = []                              # (time, session) removed early by the disk guard
_playlist_cache = {}                    # path -> (mtime_ns, segs)
_bitrate = {}                           # stream -> (t, bytesReceived)


class ClipError(Exception):
    """A failure the editor should read as-is (Thai message)."""


class BadRequest(Exception):
    pass


# ---------- settings ----------

def get_setting(key):
    try:
        raw = (SETTINGS / key).read_text().strip()
    except FileNotFoundError:
        return DEFAULTS[key]
    return int(raw)  # a corrupt file raises: loud, not a silent default


def set_setting(key, value):
    lo, hi = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise BadRequest(f'{key} ต้องเป็นจำนวนเต็ม {lo}–{hi}')
    SETTINGS.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS / (key + '.tmp')
    tmp.write_text(str(value))
    tmp.replace(SETTINGS / key)


# ---------- playlist ----------

@dataclass
class Seg:
    start: float  # unix seconds, from EXT-X-PROGRAM-DATE-TIME
    dur: float
    name: str

    @property
    def end(self):
        return self.start + self.dur


def parse_pdt(value):
    s = value.strip().replace('Z', '+00:00')
    s = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f'PROGRAM-DATE-TIME has no timezone: {value!r}')
    return dt.timestamp()


def parse_playlist(text):
    segs, pdt, dur = [], None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            pdt = parse_pdt(line.split(':', 1)[1])
        elif line.startswith('#EXTINF:'):
            dur = float(line[8:].split(',')[0])
        elif line and not line.startswith('#'):
            if dur is None:
                raise ValueError(f'segment {line} has no #EXTINF')
            if not SEG_RE.match(line):
                raise ValueError(f'unexpected segment name {line!r}')
            if pdt is None:
                if not segs:
                    raise ValueError('playlist has no #EXT-X-PROGRAM-DATE-TIME')
                pdt = segs[-1].end
            segs.append(Seg(pdt, dur, line))
            pdt = dur = None
    return segs


def read_segments(session_dir):
    pl = session_dir / 'index.m3u8'
    mtime = pl.stat().st_mtime_ns
    hit = _playlist_cache.get(str(pl))
    if hit and hit[0] == mtime:
        return hit[1]
    segs = parse_playlist(pl.read_text())
    _playlist_cache[str(pl)] = (mtime, segs)
    return segs


def plan_cut(segs, t_in, t_out):
    """Map a wall-clock range onto the segments played back to back (the concat timeline).

    Returns (segments, offset, duration, gap): offset/duration in concat seconds; gap is
    how many seconds of the requested range had no picture (encoder reconnects).
    """
    if t_out <= t_in:
        raise ClipError('จุด Out ต้องอยู่หลังจุด In')
    if not segs:
        raise ClipError('ยังไม่มีภาพในบัฟเฟอร์')
    if t_in < segs[0].start - 0.5:
        raise ClipError(f'จุด In หลุดบัฟเฟอร์ไปแล้ว ภาพเก่าสุดที่ยังมีคือ {fmt_time(segs[0].start)}')
    if t_out > segs[-1].end + 0.5:
        wait = t_out - segs[-1].end
        raise ClipError(f'ภาพยังมาไม่ถึงจุด Out อีกประมาณ {wait:.0f} วินาที ลองใหม่อีกครั้ง')
    chosen = [s for s in segs if s.start < t_out and s.end > t_in]
    if not chosen:
        raise ClipError('ช่วงนี้ไม่มีภาพเลย สัญญาณขาดตลอดช่วง')
    offset = max(0.0, t_in - chosen[0].start)
    pos, end_pos = 0.0, None
    for s in chosen:
        if t_out <= s.end:
            end_pos = pos + max(0.0, t_out - s.start)  # Out inside a gap: clamp to s.start
            break
        pos += s.dur
    if end_pos is None:
        end_pos = pos
    covered = sum(min(s.end, t_out) - max(s.start, t_in) for s in chosen)
    return chosen, offset, end_pos - offset, max(0.0, (t_out - t_in) - covered)


def fmt_time(ts):
    return datetime.fromtimestamp(ts, BKK).strftime('%H:%M:%S')


# ---------- streams / sessions ----------

def iter_sessions():
    if not DVR.exists():
        return
    for sd in sorted(DVR.iterdir()):
        if not (sd.is_dir() and NAME_RE.match(sd.name)):
            continue
        for d in sorted(sd.iterdir(), reverse=True):
            if d.is_dir() and SESSION_RE.match(d.name):
                yield sd.name, d


def session_dir(stream, session):
    if not (NAME_RE.match(stream or '') and SESSION_RE.match(session or '')):
        raise BadRequest('ชื่อสัญญาณหรือรอบไม่ถูกต้อง')
    d = DVR / stream / session
    if not (d / 'index.m3u8').exists():
        raise BadRequest('ไม่พบบัฟเฟอร์ของรอบนี้ อาจถูกลบไปแล้ว')
    return d


def mediamtx_paths():
    """{name: item} from the MediaMTX API, or None (and a warning) when it can't be reached."""
    try:
        token = base64.b64encode(f'{MEDIAMTX_API_USER}:{MEDIAMTX_API_PASS}'.encode()).decode()
        req = urllib.request.Request(MEDIAMTX_API + '/v3/paths/list?itemsPerPage=1000',
                                     headers={'Authorization': 'Basic ' + token})
        with urllib.request.urlopen(req, timeout=2) as r:
            items = json.load(r).get('items') or []
    except Exception as e:  # noqa: BLE001 - surfaced to the UI, not swallowed
        set_warning('mediamtx', f'ติดต่อ MediaMTX ไม่ได้ ({type(e).__name__}) สถานะการรับสัญญาณอาจไม่ตรง')
        return None
    set_warning('mediamtx', None)
    return {i.get('name'): i for i in items}


def build_state():
    now = time.time()
    keep = get_setting('keep_hours') * 3600
    paths = mediamtx_paths()
    streams, warn = {}, {}
    for name, d in iter_sessions():
        pl = d / 'index.m3u8'
        if not pl.exists():
            continue
        try:
            segs = read_segments(d)
        except (OSError, ValueError) as e:
            warn[f'pl:{name}/{d.name}'] = f'อ่านบัฟเฟอร์ {name}/{d.name} ไม่ได้: {e}'
            continue
        mtime = pl.stat().st_mtime
        live = now - mtime < LIVE_SECONDS
        streams.setdefault(name, []).append({
            'id': d.name, 'live': live,
            'first': segs[0].start if segs else None,
            'last': segs[-1].end if segs else None,
            'expires_at': None if live else mtime + keep,
        })
    names = sorted(set(streams) | set(paths or {}))
    out = []
    for name in names:
        item = (paths or {}).get(name) or {}
        receiving = bool(item.get('ready'))
        sessions = streams.get(name, [])
        recording = any(s['live'] for s in sessions)
        kbps = None
        if receiving and item.get('bytesReceived') is not None:
            prev = _bitrate.get(name)
            _bitrate[name] = (now, item['bytesReceived'])
            if prev and now - prev[0] >= 1 and item['bytesReceived'] >= prev[1]:
                kbps = round((item['bytesReceived'] - prev[1]) * 8 / 1000 / (now - prev[0]))
        ready_for = now - _iso_ts(item.get('readyTime')) if receiving and item.get('readyTime') else 0
        if receiving and not recording and ready_for > 30:
            warn[f'rec:{name}'] = f'{name}: รับสัญญาณอยู่แต่ไม่ได้บันทึกบัฟเฟอร์ ดู log ของ mediamtx'
        yt = youtube_state(YOUTUBE, name, now)
        if yt and yt[0] == 'failing':
            warn[f'yt:{name}'] = (f'YouTube {name}: ส่งไม่ขึ้น ({yt[1] or "ดู log ของ mediamtx"}) '
                                  'ตรวจ Stream key และไลฟ์ใน YouTube Studio')
        out.append({'name': name, 'receiving': receiving if paths is not None else None,
                    'recording': recording, 'kbps': kbps, 'sessions': sessions,
                    'youtube': yt[0] if yt else None})
    sync_warnings('pl:', {k: v for k, v in warn.items() if k.startswith('pl:')})
    sync_warnings('rec:', {k: v for k, v in warn.items() if k.startswith('rec:')})
    sync_warnings('yt:', {k: v for k, v in warn.items() if k.startswith('yt:')})
    du = shutil.disk_usage(DATA)
    with LOCK:
        warnings = list(WARNINGS.values())
    replays = load_replays()
    return {
        'now': now, 'streams': out, 'warnings': warnings,
        'settings': {k: get_setting(k) for k in DEFAULTS}, 'limits': LIMITS,
        'disk': {'free_gb': round(du.free / 1e9, 1), 'total_gb': round(du.total / 1e9, 1)},
        'storage': 'wasabi' if S3_ON else 'local',
        'srt': {'host': SRT_PUBLIC_HOST, 'port': SRT_PUBLIC_PORT},
        'ingest': INGEST_HELP,
        # every running replay (its row holds the only stop button), then the latest finished ones
        'replays': [r for r in replays if r.get('status') not in REPLAY_FINAL]
                   + [r for r in replays if r.get('status') in REPLAY_FINAL][:20],
        'replay_max': REPLAY_MAX, 'replay_max_hours': REPLAY_MAX_HOURS,
    }


def youtube_state(ydir, name, now):
    """None if the stage's YouTube push is not armed, else ('on' | 'failing', detail).
    Looks only at whether the key file exists, never at the key."""
    if not (ydir / f'{name}.key').exists():
        return None
    try:
        words = (ydir / f'{name}.status').read_text().strip().split(' ', 2)
    except OSError:
        return 'on', ''   # armed; the stage has not been live since
    # youtube.sh rewrites "failing" on every failed try (~every 15 s while live); older = stage went offline
    if words[0] == 'failing' and len(words) > 1 and words[1].isdigit() and now - int(words[1]) < 60:
        return 'failing', words[2] if len(words) > 2 else ''
    return 'on', ''


def _iso_ts(value):
    try:
        return parse_pdt(value)
    except (TypeError, ValueError):
        return time.time()


def set_warning(key, msg):
    with LOCK:
        if msg:
            if WARNINGS.get(key) != msg:
                log.warning(msg)
            WARNINGS[key] = msg
        else:
            WARNINGS.pop(key, None)


def sync_warnings(prefix, current):
    """Replace every warning under `prefix` with `current`, so fixed problems disappear."""
    with LOCK:
        for k in [k for k in WARNINGS if k.startswith(prefix) and k not in current]:
            del WARNINGS[k]
        for k, v in current.items():
            if WARNINGS.get(k) != v:
                log.warning(v)
            WARNINGS[k] = v


# ---------- clips ----------

IN_PROGRESS = ('queued', 'cutting', 'uploading')


def clip_path(cid, ext):
    return CLIPS / f'{cid}.{ext}'


def save_clip(clip):
    with LOCK:
        tmp = clip_path(clip['id'], 'json.tmp')
        tmp.write_text(json.dumps(clip, ensure_ascii=False))
        tmp.replace(clip_path(clip['id'], 'json'))


def update_clip(clip, **fields):
    clip.update(fields)
    save_clip(clip)


def load_clips():
    out = []
    for p in CLIPS.glob('*.json'):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError) as e:
            out.append({'id': p.stem, 'name': p.name, 'status': 'failed', 'created': 0,
                        'error': f'อ่านข้อมูลคลิปไม่ได้: {e}'})
    return sorted(out, key=lambda c: c.get('created', 0), reverse=True)


def load_clip(cid):
    if not CLIP_ID_RE.match(cid or ''):
        raise BadRequest('รหัสคลิปไม่ถูกต้อง')
    p = clip_path(cid, 'json')
    if not p.exists():
        raise BadRequest('ไม่พบคลิปนี้')
    return json.loads(p.read_text())


def has_local_file(clip):
    return bool(clip.get('local')) and clip_path(clip['id'], 'mp4').exists()


def safe_filename(name):
    keep = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', name).strip(' .') or 'clip'
    return keep[:120]


def submit(cid, stage):
    """Queue a job. Cuts and uploads use separate pools so a slow Wasabi never holds up cutting."""
    with LOCK:
        RUNNING.add((cid, stage))
    (CUT_POOL if stage == 'cut' else UPLOAD_POOL).submit(run_job, cid, stage)


def run_job(cid, stage):
    try:
        process_clip(cid, stage)
    except Exception as e:  # noqa: BLE001 - process_clip could not even record the failure
        log.exception('clip %s %s: result could not be recorded', cid, stage)
        set_warning(f'job:{cid}', f'คลิป {cid[:8]} บันทึกสถานะไม่ได้ ({type(e).__name__}) ดู log ของ web')
    finally:
        with LOCK:
            RUNNING.discard((cid, stage))


def create_clip(body):
    stream, session = body.get('stream'), body.get('session')
    session_dir(stream, session)
    try:
        t_in, t_out = float(body['t_in']), float(body['t_out'])
    except (KeyError, TypeError, ValueError):
        raise BadRequest('ต้องมี t_in และ t_out เป็นตัวเลขวินาที')
    name = str(body.get('name') or '').strip()[:100] or f'{stream}-{fmt_time(t_in).replace(":", "")}'
    clip = {
        'id': uuid.uuid4().hex, 'name': name, 'stream': stream, 'session': session,
        't_in': t_in, 't_out': t_out, 'accurate': bool(body.get('accurate')),
        'created': time.time(), 'status': 'queued', 'error': None, 'local': False,
        'storage': 'wasabi' if S3_ON else 'local', 's3_key': None, 'size': None,
        'duration': None, 'gap': 0, 'preroll': 0,
    }
    pin_segments(clip)  # bad range -> ClipError -> 409, nothing saved
    save_clip(clip)
    submit(clip['id'], 'cut')
    return clip


def process_clip(cid, stage):
    clip = load_clip(cid)
    try:
        if stage == 'cut':
            update_clip(clip, status='cutting', error=None)
            run_cut(clip)
            if S3_ON:
                update_clip(clip, status='uploading', error=None)  # local download works already
                submit(cid, 'upload')
                return
        elif S3_ON:
            update_clip(clip, status='uploading', error=None)
            run_upload(clip)
        update_clip(clip, status='done', error=None, failed_stage=None)
    except Exception as e:  # noqa: BLE001 - recorded on the clip, never reported as success
        failed = 'upload' if clip.get('status') == 'uploading' else 'cut'
        if isinstance(e, ClipError):
            log.warning('clip %s %s failed: %s', cid, failed, e)
            msg = str(e)
        else:
            log.exception('clip %s %s crashed', cid, failed)
            msg = f'{type(e).__name__}: {e}'
        update_clip(clip, status='failed', error=msg, failed_stage=failed)


def work_dir(clip):
    return CLIPS / f'tmp-{clip["id"]}'


def pin_segments(clip):
    """Hard-link the clip's segments into its work dir right away, so they cannot roll out of
    the buffer (or be deleted by the muxer) while the job waits in the queue."""
    sdir = DVR / clip['stream'] / clip['session']
    segs, offset, dur, gap = plan_cut(read_segments(sdir), clip['t_in'], clip['t_out'])
    work = work_dir(clip)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        lines = ['ffconcat version 1.0']
        for s in segs:
            try:
                os.link(sdir / s.name, work / s.name)
            except FileNotFoundError:
                raise ClipError('ภาพช่วงต้นคลิปเพิ่งหลุดบัฟเฟอร์ เลื่อนจุด In ให้ใหม่ขึ้น')
            except OSError:
                shutil.copy2(sdir / s.name, work / s.name)
            lines += [f"file '{s.name}'", f'duration {s.dur:.6f}']
        (work / 'list.ffconcat').write_text('\n'.join(lines) + '\n')
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    clip['plan'] = {'offset': offset, 'dur': dur, 'gap': gap}


def run_cut(clip):
    work = work_dir(clip)
    if not (work / 'list.ffconcat').exists():  # retry: pin again from the current buffer
        pin_segments(clip)
    offset, dur, gap = clip['plan']['offset'], clip['plan']['dur'], clip['plan']['gap']
    try:
        out = work / 'out.mp4'
        # Never seek on the input side: the concat demuxer's seek over MPEG-TS overshoots to
        # the next segment (clip started ~1 s late in testing). Instead:
        #  accurate: decode from the first segment and drop frames before In (output -ss),
        #            costs at most one segment of extra decoding;
        #  fast:     no seek at all, start on the first segment's keyframe, so the clip may
        #            begin up to one segment before In but never after it.
        cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'concat', '-i', str(work / 'list.ffconcat'), '-map', '0:v?', '-map', '0:a?']
        if clip['accurate']:
            preroll = 0.0
            # cfr: after a reconnect the first segment carries audio before its first video
            # keyframe; fill that hole with repeated frames so editing software sees a
            # constant frame rate instead of a variable one
            cmd += ['-ss', f'{offset:.3f}', '-t', f'{dur:.3f}', '-fps_mode', 'cfr',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '192k']
        else:
            preroll = offset
            cmd += ['-t', f'{offset + dur:.3f}', '-c', 'copy', '-avoid_negative_ts', 'make_zero']
        cmd += ['-movflags', '+faststart', str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(300, dur * 4))
        if r.returncode != 0:
            raise ClipError('ffmpeg ตัดคลิปไม่สำเร็จ: ' + (r.stderr.strip()[-400:] or f'exit {r.returncode}'))
        got, want = probe_duration(out), preroll + dur
        if abs(got - want) > 1.0:
            raise ClipError(f'ความยาวคลิปผิด ได้ {got:.1f} วินาที แต่ควรได้ {want:.1f} วินาที')
        out.replace(clip_path(clip['id'], 'mp4'))
        update_clip(clip, local=True, size=clip_path(clip['id'], 'mp4').stat().st_size,
                    duration=round(got, 2), gap=round(gap, 1), preroll=round(preroll, 2))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def probe_duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise ClipError(f'อ่านความยาวคลิปไม่ได้: {r.stderr.strip()[-200:]}')


def s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        's3', endpoint_url=S3['ENDPOINT'], region_name=S3['REGION'] or 'us-east-1',
        aws_access_key_id=S3['ACCESS_KEY'], aws_secret_access_key=S3['SECRET_KEY'],
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                      connect_timeout=10, read_timeout=60,
                      retries={'max_attempts': 3, 'mode': 'standard'}))


def s3_key(clip):
    day = datetime.fromtimestamp(clip['t_in'], BKK).strftime('%Y/%m/%d')
    stem = re.sub(r'[^A-Za-z0-9ก-๙_-]+', '-', clip['name']).strip('-')[:80] or 'clip'
    prefix = S3['PREFIX'].strip('/')
    return '/'.join(p for p in (prefix, day, clip['stream'], f'{stem}-{clip["id"][:8]}.mp4') if p)


def run_upload(clip):
    path = clip_path(clip['id'], 'mp4')
    if not path.exists():
        raise ClipError('ไฟล์คลิปในเครื่องหายไปก่อนอัปโหลด ต้องตัดใหม่')
    size = path.stat().st_size
    key = s3_key(clip)
    c = s3_client()
    c.upload_file(str(path), S3['BUCKET'], key, ExtraArgs={'ContentType': 'video/mp4'})
    remote = c.head_object(Bucket=S3['BUCKET'], Key=key)['ContentLength']
    if remote != size:
        raise ClipError(f'ไฟล์บน Wasabi มีขนาด {remote} ไบต์ ไม่ตรงกับในเครื่อง {size} ไบต์')
    update_clip(clip, s3_key=key)


def content_disposition(name):
    fname = safe_filename(name) + '.mp4'
    ascii_name = fname.encode('ascii', 'replace').decode().replace('?', '_').replace('"', '_')
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(fname)}'


def presign(clip):
    return s3_client().generate_presigned_url(
        'get_object', ExpiresIn=3600,
        Params={'Bucket': S3['BUCKET'], 'Key': clip['s3_key'],
                'ResponseContentDisposition': content_disposition(clip['name'])})


def retry_clip(cid):
    clip = load_clip(cid)
    if clip.get('status') != 'failed':
        raise BadRequest('ลองใหม่ได้เฉพาะคลิปที่ล้มเหลว')
    # an mp4 only exists after the cut passed its length check, so never cut it again
    stage = 'upload' if has_local_file(clip) else 'cut'
    update_clip(clip, status='queued', error=None, failed_stage=stage)
    submit(cid, stage)
    return clip


def stage_after_interruption(clip):
    return 'upload' if has_local_file(clip) else 'cut'


def recover_interrupted():
    """Jobs that were running when the app stopped: mark failed so nobody waits forever."""
    for clip in load_clips():
        if clip.get('status') in IN_PROGRESS:
            update_clip(clip, status='failed', failed_stage=stage_after_interruption(clip),
                        error='แอปรีสตาร์ทระหว่างทำงาน กด "ลองใหม่"')


# ---------- replay: send an ended round back out over RTMP, the way OBS plays a file ----------
# One ffmpeg per replay, -re -c copy: no re-encode (CPU ~10% of a core, same picture), one stage's
# bitrate on the VM's uplink. Copy can only start on a keyframe, so a range snaps to whole segments.
# A loop is one connection (-stream_loop): reconnecting would look like "stream ended" to YouTube.

REPLAYS = DATA / 'replay'        # <id>.json (never the stream key) + <id>/ hard links to the segments
REPLAY_MAX = int(os.environ.get('REPLAY_MAX', '2'))
REPLAY_MAX_HOURS = float(os.environ.get('REPLAY_MAX_HOURS', '12'))  # a forgotten loop stops itself
# hosts allowed although they resolve to a private address (an RTMP server on the office LAN)
REPLAY_ALLOW_HOSTS = {h.strip() for h in os.environ.get('REPLAY_ALLOW_HOSTS', '').split(',') if h.strip()}
REPLAY_FFMPEG = ['ffmpeg']       # test_replay.py swaps in a fake
REPLAY_STEADY = 30               # seconds of picture sent before a drop counts as a blip, not a refusal
REPLAY_RETRY_WAIT = 5
REPLAY_REWIND = 5                # resume this far before the last second handed to the network
REPLAY_FINAL = ('finished', 'stopped', 'failed')
REPLAY_LOCK = threading.Lock()   # guards REPLAY_RUN
REPLAY_RUN = {}                  # id -> {rec, dest, key, stop, proc, reason} while its thread runs
PROGRESS_RE = re.compile(r'^[a-z_0-9]+=\S*$')   # ffmpeg -progress lines, mixed into stderr


def host_addrs(host):
    return {ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}


def replay_dest(server, key):
    """OBS-style Server + Stream key -> (URL for ffmpeg, what the page may show). rtmp(s) to a
    public address only: a web login must not be able to make the VM probe the docker network,
    the office LAN or a metadata address (the error text would tell what answers where)."""
    server, key = str(server or '').strip(), str(key or '').strip()
    m = re.match(r'^(rtmps?)://([A-Za-z0-9.-]+)(:\d{1,5})?(/\S*)?$', server)
    if not m:
        raise BadRequest('Server ต้องเป็น rtmp://… หรือ rtmps://… เช่น rtmps://a.rtmps.youtube.com:443/live2')
    if not key or len(key) > 512 or any(c.isspace() for c in key):
        raise BadRequest('ใส่ Stream key (ห้ามมีช่องว่าง)')
    host = m[2]
    if host not in REPLAY_ALLOW_HOSTS:
        try:
            addrs = host_addrs(host)
        except OSError:
            raise BadRequest(f'หาเครื่อง {host} ไม่เจอ ตรวจชื่อใน Server อีกครั้ง')
        if not addrs or any(not ipaddress.ip_address(a.split('%')[0]).is_global for a in addrs):
            raise BadRequest(f'{host} ไม่ใช่ที่อยู่บนอินเทอร์เน็ต ส่ง replay ได้เฉพาะปลายทางภายนอก')
    return server.rstrip('/') + '/' + key, f'{m[1]}://{host}{m[3] or ""}'


def redact(line, key):
    """ffmpeg prints the destination, key included, on most errors."""
    line = re.sub(r'rtmps?://\S*', '<ปลายทาง>', line)
    return line.replace(key, '***') if len(key) >= 4 else line


def replay_segments(segs, t_in=None, t_out=None):
    """Whole segments covering t_in..t_out of an ENDED round (None = that end of the round). A
    point past either end is clamped: the round will not grow, so "wait and retry" would lie."""
    if not segs:
        raise ClipError('รอบนี้ยังไม่มีภาพ')
    t_in = segs[0].start if t_in is None else max(t_in, segs[0].start)
    t_out = segs[-1].end if t_out is None else min(t_out, segs[-1].end)
    chosen = plan_cut(segs, t_in, t_out)[0]
    if sum(s.dur for s in chosen) < 1:
        raise ClipError('ช่วงที่เลือกสั้นเกินไป')
    return chosen


def stream_params(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                        'stream=codec_type,codec_name,width,height,sample_rate,channels', '-of', 'json', str(path)],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise ClipError(f'อ่านไฟล์ {path.name} ไม่ได้: {r.stderr.strip()[-200:]}')
    return sorted(json.dumps(s, sort_keys=True) for s in json.loads(r.stdout).get('streams', []))


def replay_check_joins(d, segs):
    """dvr.sh continues a round when an encoder rejoins within RECONNECT_MIN, maybe a backup with
    other settings. Copy mode sends the first part's audio/video setup for the whole stream, so a
    different resolution or audio format after a join would play broken while the page says OK."""
    first = None
    for prev, s in zip([None] + segs[:-1], segs):
        if prev is not None and s.start <= prev.end + 1:
            continue
        p = stream_params(d / s.name)
        if first is None:
            first = p
        elif p != first:
            raise ClipError(f'เครื่องส่งต่อเข้ามาใหม่ตอน {fmt_time(s.start)} ด้วยภาพ/เสียงคนละแบบกับช่วงก่อน '
                            'ส่งรวดเดียวไม่ได้ ตั้ง In/Out แยกเป็นก่อนและหลังเวลานั้น')


def replay_index(segs, pos):
    """(index, start in seconds) of the segment that plays at `pos` seconds into the range."""
    acc = 0.0
    for i, s in enumerate(segs):
        if acc + s.dur > pos:
            return i, acc
        acc += s.dur
    return 0, 0.0


def replay_concat(segs, start, loop):
    """ffconcat for one pass from segment `start`: to the end of the range, or (loop) round to it
    again. A loop has no beginning, so starting it mid-range changes nothing on air."""
    order = segs[start:] + (segs[:start] if loop else [])
    lines = ['ffconcat version 1.0']
    for s in order:
        lines += [f"file '{s.name}'", f'duration {s.dur:.6f}']
    return '\n'.join(lines) + '\n'


def save_replay(rec):
    """Only the replay's own thread writes after creation (the janitor only after it is gone)."""
    tmp = REPLAYS / f'{rec["id"]}.json.tmp'
    tmp.write_text(json.dumps(rec, ensure_ascii=False))
    tmp.replace(REPLAYS / f'{rec["id"]}.json')


def update_replay(rec, **fields):
    rec.update(fields)
    save_replay(rec)


def load_replays():
    out = []
    for p in REPLAYS.glob('*.json'):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError) as e:
            out.append({'id': p.stem, 'status': 'failed', 'created': 0, 'error': f'อ่านข้อมูล replay ไม่ได้: {e}'})
    return sorted(out, key=lambda r: r.get('created', 0), reverse=True)


def create_replay(body):
    stream, session = body.get('stream'), body.get('session')
    d = session_dir(stream, session)
    if time.time() - (d / 'index.m3u8').stat().st_mtime < LIVE_SECONDS:
        raise ClipError('รอบนี้ยังไลฟ์อยู่ replay ได้เมื่อเวทีจบแล้ว')
    try:
        t_in = None if body.get('t_in') is None else float(body['t_in'])
        t_out = None if body.get('t_out') is None else float(body['t_out'])
    except (TypeError, ValueError):
        raise BadRequest('t_in / t_out ต้องเป็นตัวเลขวินาที')
    stop_when_live = bool(body.get('stop_when_live', True))
    dest, shown = replay_dest(body.get('server'), body.get('key'))
    segs = replay_segments(read_segments(d), t_in, t_out)
    if stop_when_live and ((mediamtx_paths() or {}).get(stream) or {}).get('ready'):
        raise ClipError(f'เวที {stream} ไลฟ์อยู่ตอนนี้ ถ้าติ๊ก "หยุดเองเมื่อเวทีนี้กลับมาไลฟ์" replay จะหยุดทันที '
                        'เอาติ๊กออกถ้าปลายทางนี้ไม่ใช่ช่องเดียวกับไลฟ์')
    replay_check_joins(d, segs)
    rid = uuid.uuid4().hex
    rec = {'id': rid, 'stream': stream, 'session': session, 't_in': segs[0].start, 't_out': segs[-1].end,
           'pass_seconds': round(sum(s.dur for s in segs), 3), 'loop': bool(body.get('loop')),
           'stop_when_live': stop_when_live, 'dest': shown,
           'status': 'starting', 'created': time.time(), 'run_started': None, 'run_from': 0.0,
           'streamed': 0.0, 'error': None, 'reason': None, 'ended': None}
    run = {'rec': rec, 'dest': dest, 'key': str(body.get('key')).strip(), 'stop': threading.Event(),
           'proc': None, 'reason': None}
    with REPLAY_LOCK:
        if len(REPLAY_RUN) >= REPLAY_MAX:
            raise ClipError(f'ส่ง replay พร้อมกันได้ไม่เกิน {REPLAY_MAX} ช่อง (แต่ละช่องใช้เน็ตขาออกเท่าบิตเรตเวที) หยุดอันเก่าก่อน')
        if any(r['dest'] == dest for r in REPLAY_RUN.values()):
            raise ClipError('มี replay ส่งไป Server + Stream key นี้อยู่แล้ว สองตัวบนคีย์เดียวกันจะเตะกันเอง')
        REPLAY_RUN[rid] = run
    work, saved = REPLAYS / rid, False
    try:
        work.mkdir(parents=True)
        # hard links: the janitor may delete the round (keep_hours) while a loop is still on air
        for s in segs:
            try:
                os.link(d / s.name, work / s.name)
            except FileNotFoundError:
                raise ClipError('ภาพบางท่อนในช่วงนี้หลุดบัฟเฟอร์ไปแล้ว เลือกช่วงใหม่')
            except OSError:
                shutil.copy2(d / s.name, work / s.name)
        save_replay(rec)
        saved = True
        threading.Thread(target=run_replay, args=(rid, segs), daemon=True, name=f'replay-{rid[:8]}').start()
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        with REPLAY_LOCK:
            REPLAY_RUN.pop(rid, None)
        if saved:  # never leave a record saying "starting" that no thread will move on
            try:
                update_replay(rec, status='failed', ended=time.time(), error='เริ่ม replay ไม่ได้ ดู log ของ web')
            except OSError:
                pass
        raise
    return rec


def run_replay(rid, segs):
    run = REPLAY_RUN[rid]
    rec, work, pass_s = run['rec'], REPLAYS / rid, run['rec']['pass_seconds']
    pos, ok_once, quick = 0.0, False, 0   # pos: seconds into the range where this ffmpeg starts
    try:
        while not run['stop'].is_set():
            start, pos = replay_index(segs, pos)
            (work / 'list.ffconcat').write_text(replay_concat(segs, start, rec['loop']))
            want = None if rec['loop'] else pass_s - pos
            # ffmpeg 8 checks no TLS certificate unless told to (see cloud/rtmp/youtube.sh)
            tls = ['-tls_verify', '1'] if run['dest'].startswith('rtmps://') else []
            loop = ['-stream_loop', '-1'] if rec['loop'] else []
            cmd = REPLAY_FFMPEG + ['-nostdin', '-hide_banner', '-loglevel', 'warning', '-progress', 'pipe:2',
                                   *loop, '-re', '-f', 'concat', '-i', 'list.ffconcat', '-map', '0:v?',
                                   '-map', '0:a?', '-c', 'copy', '-f', 'flv', *tls, '-rw_timeout', '10000000',
                                   run['dest']]
            update_replay(rec, status='starting', run_started=None, run_from=pos)   # connecting
            proc = subprocess.Popen(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, errors='replace')
            run['proc'] = proc
            if run['stop'].is_set():  # stop pressed while this ffmpeg was starting
                proc.terminate()
            tail = deque(maxlen=4)    # the cause is a few lines up: the last says only "I/O error"
            sent = 0.0                # seconds of the range ffmpeg has handed to the network
            for line in proc.stderr:
                line = line.strip()
                if PROGRESS_RE.match(line):
                    k, _, v = line.partition('=')
                    if k == 'out_time_us' and v.isdigit():
                        if not sent and int(v):
                            # progress comes only after the output opened: the RTMP server took
                            # the publish and picture is leaving. Not before: no "sending" on a hope.
                            update_replay(rec, status='streaming', error=None,
                                          run_started=time.time() - int(v) / 1e6)
                        sent = int(v) / 1e6
                    continue
                line = redact(line, run['key'])
                if line:
                    tail.append(line[-200:])
                    log.info('replay %s: %s', rid[:8], line[-300:])
            rc = proc.wait()
            rec['streamed'] = round(rec['streamed'] + sent, 1)
            if run['stop'].is_set():
                break
            if want is not None and rc == 0 and sent >= want - 3:
                update_replay(rec, status='finished', run_started=None, ended=time.time())
                return
            # anything else is a drop: a loop never ends by itself; a pass that ended short did not finish
            if rc == 0:
                err = f'ffmpeg หยุดก่อนครบ (ส่งไป {sent:.0f} วิ)'
            else:
                err = ' / '.join(tail) or f'ffmpeg จบด้วยรหัส {rc}'
            ok_once = ok_once or sent > 0                     # the destination took this key once
            quick = 0 if sent >= REPLAY_STEADY else quick + 1
            if not ok_once or quick >= 3:
                hint = ('ส่งไม่ขึ้นตั้งแต่แรก: ตรวจ Server กับ Stream key และว่าปลายทางเปิดรับไลฟ์แล้ว'
                        if not ok_once else 'สายหลุดซ้ำ 3 ครั้งติด')
                update_replay(rec, status='failed', error=f'{hint} ({err})', run_started=None, ended=time.time())
                return
            nxt = pos + max(0.0, sent - REPLAY_REWIND)
            # one pass never wraps back to the start: at worst its last segment goes out again
            pos = nxt % pass_s if rec['loop'] else min(nxt, pass_s - segs[-1].dur)
            update_replay(rec, status='retrying', error=err, run_started=None)
            run['stop'].wait(REPLAY_RETRY_WAIT)
        update_replay(rec, status='stopped', reason=run['reason'], run_started=None, ended=time.time())
    except Exception as e:  # noqa: BLE001 - recorded on the replay, never left "streaming"
        log.exception('replay %s crashed', rid[:8])
        try:
            update_replay(rec, status='failed', error=f'{type(e).__name__}: {e}', run_started=None,
                          ended=time.time())
        except OSError:
            pass  # disk full: the janitor marks it failed once it can write again
    finally:
        proc = run.get('proc')
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)
        with REPLAY_LOCK:
            REPLAY_RUN.pop(rid, None)


def stop_replay(rid, reason='มีคนกดหยุด'):
    with REPLAY_LOCK:
        run = REPLAY_RUN.get(rid)
    if not run:
        raise BadRequest('replay นี้ไม่ได้ส่งอยู่แล้ว')
    run['reason'] = reason
    run['stop'].set()
    proc = run.get('proc')
    if proc and proc.poll() is None:
        proc.terminate()  # rtmp output: ffmpeg closes the stream cleanly; kill if it hangs
        threading.Timer(15, lambda: proc.poll() is None and proc.kill()).start()
    return run['rec']


def replay_watch_once(now=None):
    """Stops a replay that has been on air REPLAY_MAX_HOURS (a loop nobody remembered eats the
    VM's traffic allowance), and one whose stage is back on air (two feeds on one key fight)."""
    now = now or time.time()
    with REPLAY_LOCK:
        runs = [(rid, r['rec']) for rid, r in REPLAY_RUN.items() if not r['stop'].is_set()]
    for rid, rec in runs:
        on_air = rec['streamed'] + (now - rec['run_started'] if rec.get('run_started') else 0)
        if on_air > REPLAY_MAX_HOURS * 3600:
            try:
                stop_replay(rid, f'ส่งครบ {REPLAY_MAX_HOURS:g} ชม. หยุดให้เอง (ค่า REPLAY_MAX_HOURS)')
            except BadRequest:
                pass  # it ended by itself in between
    runs = [(rid, rec) for rid, rec in runs if rec['stop_when_live']]
    if not runs:
        return
    paths = mediamtx_paths()
    if paths is None:
        return  # cannot tell (a warning is already up): keep sending rather than guess
    for rid, rec in runs:
        if (paths.get(rec['stream']) or {}).get('ready'):
            try:
                stop_replay(rid, f'เวที {rec["stream"]} กลับมาไลฟ์ หยุด replay ให้เอง')
            except BadRequest:
                pass


def replay_watch_loop():
    while True:
        try:
            replay_watch_once()
        except Exception:  # noqa: BLE001
            log.exception('replay watch failed')
        time.sleep(5)


def recover_replays():
    """At startup nothing is sending: a record still 'streaming' died with the old process."""
    for rec in load_replays():
        if rec.get('status') not in REPLAY_FINAL:
            update_replay(rec, status='failed', run_started=None, ended=time.time(),
                          error='แอปรีสตาร์ทระหว่างส่ง replay กดเริ่มใหม่ (ใส่ Stream key อีกครั้ง)')
    for d in REPLAYS.glob('*'):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def janitor_replays(now):
    with REPLAY_LOCK:
        running = set(REPLAY_RUN)
    for rec in load_replays():
        p = REPLAYS / f'{rec["id"]}.json'
        if rec.get('status') in REPLAY_FINAL:   # finished replays stay listed for a day
            when = rec.get('ended') or rec.get('created') or (p.stat().st_mtime if p.exists() else now)
            if now - when > 24 * 3600:
                p.unlink(missing_ok=True)
        elif rec['id'] not in running and p.exists() and now - p.stat().st_mtime > 120:
            # its thread is gone without writing an end (disk was full): say so, never "sending"
            update_replay(rec, status='failed', run_started=None, ended=now,
                          error='replay หยุดกลางคันโดยไม่ได้บันทึกผล ดู log ของ web')


# ---------- janitor ----------

def janitor_once(now=None):
    now = now or time.time()
    keep = get_setting('keep_hours') * 3600
    reconnect_s = RECONNECT_MIN * 60
    ended = []  # (mtime, dir): past the reconnect window, so no encoder will continue them
    empty = {}
    for _, d in list(iter_sessions()):
        pl = d / 'index.m3u8'
        mtime = (pl if pl.exists() else d).stat().st_mtime
        if now - mtime > keep:
            log.info('janitor: removing expired buffer %s', d)
            shutil.rmtree(d, ignore_errors=True)
            continue
        if now - mtime >= reconnect_s:
            ended.append((mtime, d))
        if pl.exists():
            try:
                referenced = {s.name for s in read_segments(d)}
            except (OSError, ValueError):
                continue  # unreadable playlist: never delete segments blind
            if not referenced:
                # an empty playlist next to segment files is damage, not "nothing to keep"
                if any(d.glob('seg_*.ts')):
                    empty[f'empty:{d}'] = f'บัฟเฟอร์ {d.parent.name}/{d.name} มีไฟล์ภาพแต่รายการว่าง ไม่ได้ลบอะไร'
                continue
            for f in d.glob('seg_*.ts'):
                if f.name not in referenced and now - f.stat().st_mtime > ORPHAN_SECONDS:
                    f.unlink(missing_ok=True)
    for sd in DVR.glob('*') if DVR.exists() else []:
        if sd.is_dir() and not any(sd.iterdir()):
            sd.rmdir()

    with LOCK:
        running = {cid for cid, _ in RUNNING}
    for c in load_clips():
        cid = c['id']
        p = clip_path(cid, 'mp4')
        # Only copies that are safely on Wasabi are removed; a local-only clip is the only copy.
        if (c.get('status') == 'done' and c.get('s3_key') and p.exists()
                and now - c.get('created', now) > LOCAL_CLIP_HOURS * 3600):
            p.unlink()
            update_clip(c, local=False)
        # A job that is "in progress" but not in any pool died without recording its result.
        if c.get('status') in IN_PROGRESS and cid not in running and CLIP_ID_RE.match(cid):
            jp = clip_path(cid, 'json')
            if jp.exists() and now - jp.stat().st_mtime > 120:
                update_clip(c, status='failed', failed_stage=stage_after_interruption(c),
                            error='งานหยุดกลางคันโดยไม่ได้บันทึกผล กด "ลองใหม่"')
    for t in CLIPS.glob('tmp-*'):
        if t.name[4:] not in running and now - t.stat().st_mtime > 6 * 3600:
            shutil.rmtree(t, ignore_errors=True)
    janitor_replays(now)

    # disk guard: the volume shares a host with other services; never let it fill up
    free_gb = shutil.disk_usage(DATA).free / 1e9
    for _, d in sorted(ended):
        if free_gb >= MIN_FREE_GB:
            break
        log.warning('janitor: disk low (%.1f GB), removing ended buffer %s early', free_gb, d)
        shutil.rmtree(d, ignore_errors=True)
        EARLY.append((now, f'{d.parent.name}/{d.name}'))
        free_gb = shutil.disk_usage(DATA).free / 1e9
    if free_gb < MIN_FREE_GB:
        held = f' (replay {len(REPLAY_RUN)} ตัวที่กำลังส่งถือไฟล์ของรอบที่เล่นไว้ หยุดแล้วพื้นที่จะคืน)' if REPLAY_RUN else ''
        set_warning('disk', f'พื้นที่ดิสก์เหลือ {free_gb:.1f} GB ต่ำกว่า {MIN_FREE_GB:.0f} GB ลดบัฟเฟอร์หรือเพิ่มดิสก์{held}')
    else:
        set_warning('disk', None)
    EARLY[:] = [e for e in EARLY if now - e[0] < 6 * 3600]
    set_warning('disk_early', 'ลบบัฟเฟอร์ก่อนเวลาเพราะดิสก์ใกล้เต็ม: ' + ', '.join(n for _, n in EARLY) if EARLY else None)

    sync_warnings('empty:', {k: v for k, v in empty.items() if Path(k[len('empty:'):]).exists()})
    for k in list(_playlist_cache):
        if not Path(k).exists():
            _playlist_cache.pop(k, None)


def janitor_loop():
    while True:
        try:
            janitor_once()
            set_warning('janitor', None)
        except Exception as e:  # noqa: BLE001 - keep looping, but say so in the UI
            log.exception('janitor failed')
            set_warning('janitor', f'งานลบไฟล์เก่าล้มเหลว: {type(e).__name__}: {e}')
        time.sleep(60)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = 'live-clip'
    protocol_version = 'HTTP/1.1'

    timeout = 60  # idle or half-open connections must not hold a thread forever

    def log_message(self, fmt, *args):
        log.info('%s %s', self.address_string(), fmt % args)

    def log_request(self, code='-', size='-'):
        # the UI polls every few seconds; only log what someone would act on
        polling = self.path.startswith('/hls/') or self.path in ('/api/state', '/api/clips', '/healthz')
        if polling and str(code)[:1] in '23':
            return
        super().log_request(code, size)

    def authorized(self):
        h = self.headers.get('Authorization', '')
        if not h.startswith('Basic '):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:], validate=True).decode().partition(':')
        except (ValueError, UnicodeDecodeError):
            return False
        ok_user = hmac.compare_digest(user.encode(), WEB_USER.encode())
        ok_pw = hmac.compare_digest(pw.encode(), WEB_PASSWORD.encode())
        return ok_user and ok_pw

    def send(self, code, body=b'', ctype='text/plain; charset=utf-8', headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8',
                  {'Cache-Control': 'no-store'})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/healthz':
            return self.send(200, 'ok')
        if not self.authorized():
            return self.send(401, 'login required', headers={'WWW-Authenticate': 'Basic realm="live-clip", charset="UTF-8"'})
        try:
            if path in ('/', '/index.html'):
                return self.send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8',
                                 {'Cache-Control': 'no-store'})
            if path == '/api/state':
                return self.json(build_state())
            if path == '/api/clips':
                return self.json(load_clips()[:300])
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[0] == 'hls':
                return self.serve_hls(*parts[1:])
            if len(parts) == 3 and parts[0] == 'clips' and parts[2] == 'download':
                return self.download(parts[1])
            self.send(404, 'not found')
        except BadRequest as e:
            self.json({'error': str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_hls(self, stream, session, fname):
        d = session_dir(stream, session)
        if fname == 'index.m3u8':
            text = (d / 'index.m3u8').read_text()
            live = time.time() - (d / 'index.m3u8').stat().st_mtime < LIVE_SECONDS
            # some browsers can't parse "+0000"; give hls.js "+00:00"
            text = PDT_OFFSET_RE.sub(r'\1:\2', text)
            if not live and '#EXT-X-ENDLIST' not in text:
                text = text.rstrip('\n') + '\n#EXT-X-ENDLIST\n'  # ended: let the player treat it as VOD
            body, headers = text.encode(), {'Cache-Control': 'no-store'}
            if 'gzip' in self.headers.get('Accept-Encoding', ''):
                body, headers['Content-Encoding'] = gzip.compress(body, 5), 'gzip'
            return self.send(200, body, 'application/vnd.apple.mpegurl', headers)
        if not SEG_RE.match(fname):
            raise BadRequest('ชื่อไฟล์ไม่ถูกต้อง')
        try:
            data = (d / fname).read_bytes()
        except FileNotFoundError:
            return self.send(404, 'segment rolled out of the buffer')
        self.send(200, data, 'video/mp2t', {'Cache-Control': 'private, max-age=86400'})

    def download(self, cid):
        clip = load_clip(cid)
        p = clip_path(cid, 'mp4')
        if p.exists() and clip.get('local'):
            size = p.stat().st_size
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', content_disposition(clip['name']))
            self.end_headers()
            if self.command != 'HEAD':
                with p.open('rb') as f:
                    shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            return
        if clip.get('s3_key') and S3_ON:
            return self.send(302, '', headers={'Location': presign(clip), 'Cache-Control': 'no-store'})
        self.send(404, 'ไฟล์คลิปนี้ไม่มีแล้ว', )

    def do_POST(self):
        if not self.authorized():
            return self.send(401, 'login required', headers={'WWW-Authenticate': 'Basic realm="live-clip", charset="UTF-8"'})
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            return self.json({'error': 'cross-origin request refused'}, 403)
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.json({'error': 'Content-Type must be application/json'}, 415)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 10_000:
                raise BadRequest('request too large')
            body = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body, dict):
                raise BadRequest('body must be a JSON object')
            path = urlparse(self.path).path
            parts = path.strip('/').split('/')
            if path == '/api/clips':
                return self.json(create_clip(body), 201)
            if len(parts) == 4 and parts[:2] == ['api', 'clips'] and parts[3] == 'retry':
                return self.json(retry_clip(parts[2]))
            if path == '/api/replays':
                return self.json(create_replay(body), 201)
            if len(parts) == 4 and parts[:2] == ['api', 'replays'] and parts[3] == 'stop':
                if not CLIP_ID_RE.match(parts[2]):
                    raise BadRequest('รหัส replay ไม่ถูกต้อง')
                return self.json(stop_replay(parts[2]))
            if path == '/api/settings':
                for key in DEFAULTS:
                    if key in body:
                        set_setting(key, body[key])
                return self.json({k: get_setting(k) for k in DEFAULTS})
            self.json({'error': 'not found'}, 404)
        except ClipError as e:
            self.json({'error': str(e)}, 409)
        except (BadRequest, ValueError) as e:
            self.json({'error': str(e)}, 400)


def check_config():
    problems = []
    if not WEB_USER or len(WEB_PASSWORD) < 12:
        problems.append('WEB_USER and WEB_PASSWORD (12+ chars) are required')
    if not MEDIAMTX_API_PASS:
        problems.append('MEDIAMTX_API_PASS is required')
    for k in ('WEB_PASSWORD', 'SRT_PASSPHRASE', 'MEDIAMTX_API_PASS'):
        if 'change-me' in os.environ.get(k, ''):
            problems.append(f'{k} still has the example value')
    partial = [k for k in S3_REQUIRED if not S3[k]]
    if 0 < len(partial) < len(S3_REQUIRED):
        problems.append('S3 is half-configured, missing: ' + ', '.join('S3_' + k for k in partial))
    if problems:
        raise SystemExit('live-clip refuses to start: ' + '; '.join(problems))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    check_config()
    for d in (DVR, CLIPS, SETTINGS, REPLAYS):
        d.mkdir(parents=True, exist_ok=True)
    recover_interrupted()
    recover_replays()
    threading.Thread(target=janitor_loop, daemon=True).start()
    threading.Thread(target=replay_watch_loop, daemon=True).start()
    port = int(os.environ.get('PORT', '8080'))
    log.info('live-clip on :%d, storage=%s', port, 'wasabi' if S3_ON else 'local only')
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()


if __name__ == '__main__':
    main()
