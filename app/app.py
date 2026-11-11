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


class ClipGone(BadRequest):
    """The clip was deleted while a writer still held its record."""

    def __init__(self):
        super().__init__('ไม่พบคลิปนี้ (ถูกลบไปแล้ว)')


class Forbidden(Exception):
    """Wrong or missing RESTREAM_PIN."""


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
        'restream': restream_state(names),
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


def _write_clip(clip):   # caller holds LOCK
    tmp = clip_path(clip['id'], 'json.tmp')
    tmp.write_text(json.dumps(clip, ensure_ascii=False))
    tmp.replace(clip_path(clip['id'], 'json'))


def save_clip(clip):
    with LOCK:
        _write_clip(clip)


def update_clip(clip, **fields):
    """Change a clip that exists. One deleted meanwhile stays deleted: a late writer (a retry,
    the janitor) holding the old record must not bring it back into the list."""
    clip.update(fields)
    with LOCK:
        if not clip_path(clip['id'], 'json').exists():
            raise ClipGone()
        _write_clip(clip)


def load_clips():
    out = []
    for p in CLIPS.glob('*.json'):
        try:
            out.append(json.loads(p.read_text()))
        except FileNotFoundError:
            continue  # deleted between the listing and this read: gone, not damaged
        except (OSError, ValueError) as e:
            out.append({'id': p.stem, 'name': p.name, 'status': 'failed', 'created': 0,
                        'error': f'อ่านข้อมูลคลิปไม่ได้: {e}'})
    return sorted(out, key=lambda c: c.get('created', 0), reverse=True)


def load_clip(cid):
    if not CLIP_ID_RE.match(cid or ''):
        raise BadRequest('รหัสคลิปไม่ถูกต้อง')
    try:
        return json.loads(clip_path(cid, 'json').read_text())
    except FileNotFoundError:
        raise BadRequest('ไม่พบคลิปนี้')


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
    # One step under LOCK: a second retry (double click, two editors) sees 'queued' and is refused,
    # so a clip never has two jobs. delete_clip relies on that: no job in RUNNING means none exists.
    with LOCK:
        clip = load_clip(cid)
        if clip.get('status') != 'failed':
            raise BadRequest('ลองใหม่ได้เฉพาะคลิปที่ล้มเหลว')
        if any(c == cid for c, _ in RUNNING):  # the failed job wrote its result but has not left RUNNING yet
            raise BadRequest('งานก่อนหน้ายังปิดไม่เสร็จ กดลองใหม่อีกครั้ง')
        # an mp4 only exists after the cut passed its length check, so never cut it again
        stage = 'upload' if has_local_file(clip) else 'cut'
        clip.update(status='queued', error=None, failed_stage=stage)
        _write_clip(clip)
        RUNNING.add((cid, stage))
    (CUT_POOL if stage == 'cut' else UPLOAD_POOL).submit(run_job, cid, stage)
    return clip


def delete_clip(cid, seen=None):
    """Remove a clip for good: the file on this server, its record and any leftover work dir.
    A copy that already reached Wasabi is left in the bucket; the reply names it, so nobody
    takes "deleted" to mean the archive copy is gone too. `seen` is the status the page was
    showing: lists are up to 10 s old, and a "failed" row someone else has just retried into a
    finished clip must not be deleted on the strength of the old row."""
    if not CLIP_ID_RE.match(cid or ''):
        raise BadRequest('รหัสคลิปไม่ถูกต้อง')
    jp = clip_path(cid, 'json')
    with LOCK:  # one step against retry and the janitor: they write through update_clip, under this lock
        try:
            clip = json.loads(jp.read_text())
        except FileNotFoundError:
            raise BadRequest('ไม่พบคลิปนี้')
        except ValueError:
            clip = None  # a record nobody can read is exactly what someone wants out of the list
        if not isinstance(clip, dict):
            clip = {}
        if clip.get('status') in IN_PROGRESS or any(c == cid for c, _ in RUNNING):
            raise ClipError('คลิปนี้กำลังตัดหรืออัปโหลดอยู่ รอให้เสร็จหรือขึ้นว่าล้มเหลวก่อน แล้วค่อยลบ')
        if seen is not None and clip and clip.get('status') != seen:
            raise ClipError('สถานะของคลิปนี้เปลี่ยนไปแล้ว ดูรายการใหม่ก่อนลบ')
        kept, maybe = clip.get('s3_key') or None, None
        if not kept and clip.get('storage') == 'wasabi' and clip.get('failed_stage') == 'upload':
            try:
                maybe = s3_key(clip)  # an upload can land without being recorded (the check after it failed)
            except (KeyError, TypeError, ValueError):
                pass
        # the file first: if it cannot be removed the record stays, and the clip is still in the
        # list to try again, instead of an mp4 nobody can see filling the disk
        clip_path(cid, 'mp4').unlink(missing_ok=True)
        jp.unlink()
    shutil.rmtree(CLIPS / f'tmp-{cid}', ignore_errors=True)
    log.info('clip %s deleted (%r) wasabi=%r maybe=%r', cid, clip.get('name'), kept, maybe)
    reply = {'ok': True, 'wasabi_kept': kept}
    if maybe:
        reply['wasabi_maybe'] = maybe
    return reply


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
REPLAY_RUN = {}                  # id -> {rec, dest, stop, proc, reason} while its thread runs
PROGRESS_RE = re.compile(r'^[a-z_0-9]+=\s*\S*$')   # ffmpeg -progress lines (values may be padded), mixed into stderr
RESTREAM_RUN = {}                # (stage, id) -> run, see the restream section


def dest_in_use(dest):
    """True while a replay or a restream is sending to this server + key. Callers hold their own
    lock; the two registries are only read here."""
    return (any(r['dest']['ident'] == dest['ident'] for r in list(REPLAY_RUN.values()))
            or any(r['dest']['ident'] == dest['ident'] for r in list(RESTREAM_RUN.values())))


def host_addrs(host):
    return {ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}


def rtmp_dest(server, key, resolve=True):
    """OBS-style Server + Stream key, checked -> what ffmpeg needs and what the page may show.
    rtmp(s) only, and the name must resolve to public addresses when checked: a web login must not
    be able to make the VM probe the docker network, the office LAN or a metadata address (the error
    text would tell what answers where). Checked again before every connection; it cannot stop a
    name that changes its answer between this check and ffmpeg's own lookup.
    The key never goes into the URL: ffmpeg cuts an RTMP URL's path+query at 1023 characters without
    a word (char path[1024] in rtmpproto.c) and TikTok's signed keys are longer. -rtmp_playpath has
    no such limit (4,000 characters arrived intact, 10 Nov 2026) and skips ffmpeg's ".mp4"/".flv"
    rewriting of the key; app = the server's whole path, as OBS sends it."""
    server, key = str(server or '').strip(), str(key or '').strip()
    m = re.match(r'^(rtmps?)://([A-Za-z0-9.-]+)(:\d{1,5})?(/[\x21-\x7e]*)?$', server)
    if not m:
        raise BadRequest('Server ต้องเป็น rtmp://… หรือ rtmps://… เช่น rtmps://a.rtmps.youtube.com:443/live2')
    if not re.fullmatch(r'[\x21-\x7e]{1,4096}', key):   # printable ASCII: no spaces, no control characters
        raise BadRequest('ใส่ Stream key (ตัวอักษรอังกฤษ ตัวเลข และสัญลักษณ์เท่านั้น ห้ามมีช่องว่าง ยาวไม่เกิน 4,096 ตัว)')
    host = m[2]
    if resolve and host not in REPLAY_ALLOW_HOSTS:
        try:
            addrs = host_addrs(host)
        except OSError:
            raise BadRequest(f'หาเครื่อง {host} ไม่เจอ ตรวจชื่อใน Server อีกครั้ง')
        if not addrs or any(not ipaddress.ip_address(a.split('%')[0]).is_global for a in addrs):
            raise BadRequest(f'{host} ไม่ใช่ที่อยู่บนอินเทอร์เน็ต ส่งออกได้เฉพาะปลายทางภายนอก')
    base, app = f'{m[1]}://{host}{m[3] or ""}', (m[4] or '').strip('/')
    expire = re.search(r'[?&]expire=(\d{10})(?:&|$)', key)   # TikTok signs an expiry into the key
    port = int(m[3][1:]) if m[3] else 443 if m[1] == 'rtmps' else 1935
    return {'url': f'{base}/{app}', 'app': app, 'key': key, 'shown': base, 'server': server,
            # one push per server+key: "HOST", "host." and the default port are the same server
            'ident': f'{m[1]}://{host.lower().rstrip(".")}:{port}/{app}/{key}',
            'expires': int(expire[1]) if expire else None}


def rtmp_out_args(dest):
    # ffmpeg 8 checks no TLS certificate unless told to (see cloud/rtmp/youtube.sh)
    tls = ['-tls_verify', '1'] if dest['url'].startswith('rtmps://') else []
    # no_duration_filesize: RTMP cannot seek, so without it every ending prints two warnings that
    # push the real cause out of the lines the operator gets to read
    return ['-f', 'flv', '-flvflags', 'no_duration_filesize', '-rtmp_app', dest['app'], '-rtmp_playpath', dest['key'],
            *tls, '-rw_timeout', '10000000', dest['url']]


REPEATED_RE = re.compile(r'^Error opening (in|out)put files?\b')   # ffmpeg says it three times; the first is the specific one


def tidy(line):
    """An ffmpeg log line for the page: "[tls @ 0x7f34...] IO error" -> "[tls] IO error"."""
    return re.sub(r'\[([^\]@]+?) @ 0x[0-9a-f]+\]', r'[\1]', line)


def redact(line, *secrets):
    """ffmpeg prints its URLs on most errors: the destination, and the MediaMTX login in rtsp://.
    Every string that reaches the log or the page goes through here, exception text included."""
    line = re.sub(r'[a-z][a-z0-9+.-]*://\S*', lambda m: '<ปลายทาง>' if m[0].startswith('rtmp') else '<ที่อยู่>', line)
    for secret in secrets:
        if secret and len(secret) >= 4:
            line = line.replace(secret, '***')
    return line


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


RESTREAM_HOLDS_UPLINK = ('กำลังส่งเวทีสดออกหลายช่องอยู่ (Restream) เน็ตขาออกของเซิร์ฟเวอร์สงวนไว้ให้ไลฟ์จริง '
                         'เริ่ม Replay ได้เมื่อ Restream หยุดแล้ว')


def create_replay(body):
    stream, session = body.get('stream'), body.get('session')
    d = session_dir(stream, session)
    if RESTREAM_RUN:   # said first, before any probing; checked again under the lock below
        raise ClipError(RESTREAM_HOLDS_UPLINK)
    if time.time() - (d / 'index.m3u8').stat().st_mtime < LIVE_SECONDS:
        raise ClipError('รอบนี้ยังไลฟ์อยู่ replay ได้เมื่อเวทีจบแล้ว')
    try:
        t_in = None if body.get('t_in') is None else float(body['t_in'])
        t_out = None if body.get('t_out') is None else float(body['t_out'])
    except (TypeError, ValueError):
        raise BadRequest('t_in / t_out ต้องเป็นตัวเลขวินาที')
    stop_when_live = bool(body.get('stop_when_live', True))
    dest = rtmp_dest(body.get('server'), body.get('key'))
    segs = replay_segments(read_segments(d), t_in, t_out)
    if stop_when_live and ((mediamtx_paths() or {}).get(stream) or {}).get('ready'):
        raise ClipError(f'เวที {stream} ไลฟ์อยู่ตอนนี้ ถ้าติ๊ก "หยุดเองเมื่อเวทีนี้กลับมาไลฟ์" replay จะหยุดทันที '
                        'เอาติ๊กออกถ้าปลายทางนี้ไม่ใช่ช่องเดียวกับไลฟ์')
    replay_check_joins(d, segs)
    rid = uuid.uuid4().hex
    rec = {'id': rid, 'stream': stream, 'session': session, 't_in': segs[0].start, 't_out': segs[-1].end,
           'pass_seconds': round(sum(s.dur for s in segs), 3), 'loop': bool(body.get('loop')),
           'stop_when_live': stop_when_live, 'dest': dest['shown'],
           'status': 'starting', 'created': time.time(), 'run_started': None, 'run_from': 0.0,
           'streamed': 0.0, 'error': None, 'reason': None, 'ended': None}
    run = {'rec': rec, 'dest': dest, 'stop': threading.Event(), 'proc': None, 'reason': None}
    with REPLAY_LOCK:
        if RESTREAM_RUN:
            raise ClipError(RESTREAM_HOLDS_UPLINK)
        if len(REPLAY_RUN) >= REPLAY_MAX:
            raise ClipError(f'ส่ง replay พร้อมกันได้ไม่เกิน {REPLAY_MAX} ช่อง (แต่ละช่องใช้เน็ตขาออกเท่าบิตเรตเวที) หยุดอันเก่าก่อน')
        if dest_in_use(dest):
            raise ClipError('มีการส่งไป Server + Stream key นี้อยู่แล้ว สองตัวบนคีย์เดียวกันจะเตะกันเอง')
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
            try:
                run['dest'] = rtmp_dest(run['dest']['server'], run['dest']['key'])   # the name must still be public
            except BadRequest as e:
                update_replay(rec, status='failed', error=str(e), run_started=None, ended=time.time())
                return
            (work / 'list.ffconcat').write_text(replay_concat(segs, start, rec['loop']))
            want = None if rec['loop'] else pass_s - pos
            loop = ['-stream_loop', '-1'] if rec['loop'] else []
            cmd = REPLAY_FFMPEG + ['-nostdin', '-hide_banner', '-loglevel', 'warning', '-progress', 'pipe:2',
                                   *loop, '-re', '-f', 'concat', '-i', 'list.ffconcat', '-map', '0:v?',
                                   '-map', '0:a?', '-c', 'copy', *rtmp_out_args(run['dest'])]
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
                line = redact(line, run['dest']['key'])
                if line:
                    log.info('replay %s: %s', rid[:8], line[-300:])
                    if not REPEATED_RE.match(line):
                        tail.append(tidy(line)[-200:])
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


# ---------- restream: push a LIVE stage to up to RESTREAM_MAX platforms at once ----------
# One ffmpeg per destination, -c copy: no re-encode (about 11% of a core each, measured 10 Nov 2026).
# It reads the stage back from MediaMTX over SRT, like dvr.sh: that keeps the encoder's own audio/
# video timing. RTSP is only the fallback for an encoder whose messy timestamps make MediaMTX drop
# SRT readers (4 Nov 2026): through RTSP, MediaMTX re-times each track by itself and audio lands
# 40-240 ms late depending on B-frames (measured 10 Nov 2026), so it is never the first choice.
# Unlike replay, destinations AND their stream keys are saved on this server (0600):
# a live push has to survive an encoder reconnect and an app restart. So control needs RESTREAM_PIN
# on top of the web login, because that login is shared with every editor's machine.
#
# What this can know: the platform's ingest took the stream and bytes are leaving. It cannot know
# that the broadcast is public (Facebook needs "Go live", a YouTube event may not have started), so
# no status here says "on air".

RESTREAM = DATA / 'restream'       # <stage>.json: saved destinations with keys; <stage>.log: events
RESTREAM_PIN = os.environ.get('RESTREAM_PIN', '')
RESTREAM_MAX = int(os.environ.get('RESTREAM_MAX', '4'))     # pushes at once, all stages, replays included
RESTREAM_PER_STAGE = 4
RESTREAM_SOURCE = os.environ.get('RESTREAM_SOURCE', '')     # replaces the SRT read-back, {stage} = stage name
RESTREAM_FRESH = 6       # "sending" is shown only while bytes left within this many seconds
RESTREAM_STALL = 20      # no bytes leaving for this long: kill the ffmpeg, it will not recover by itself
RESTREAM_CONNECT = 30    # this long without the first picture leaving: kill it
RESTREAM_LAG_WINDOW = 60 # look at this much of a push at a time ...
RESTREAM_LAG_MAX = 5     # ... and kill it if it sent this many seconds less than real time (see watch)
RESTREAM_READ_ENDS = 3   # the SRT read ending this often within a minute, stage fine: read over RTSP
RESTREAM_WARN_AFTER = 30 # a channel that cannot hold a stream for this long goes into the yellow bar
RESTREAM_GIVEUP = 300    # a destination that had worked is retried for this long, then failed
RESTREAM_BACKOFF = (2, 5, 10, 20, 30)
RESTREAM_TICK = 2
RESTREAM_SAMPLE_GAP = 1  # between the two looks at a stage's byte counter when Start is pressed
RESTREAM_MIN_GAP = 0.3   # two looks closer than this cannot tell a moving counter from a still one
RESTREAM_LOCK = threading.RLock()  # guards RESTREAM_RUN, STAGE_FLOW, RS_LOG and the destination files
STAGE_FLOW = {}                    # stage -> {ready, flowing, bytes, tracks, t}, sampled by the watcher
FLOW_ASKED = [False]               # has MediaMTX answered the last time it was asked?
RS_LOG = {}                        # stage -> deque of the latest events (also appended to <stage>.log)
PIN_FAILS = []                     # times of wrong PINs (RESTREAM_LOCK)
RS_ON = ('connecting', 'streaming', 'retrying', 'waiting', 'stopping')
RTSP_NOTE = ('เครื่องส่งของเวทีมี timestamp ไม่เรียง จึงอ่านสัญญาณแบบสำรอง (RTSP): ภาพไม่ขาด '
             'แต่เสียงที่ปลายทางอาจช้ากว่าภาพ 0.04-0.24 วินาที ควรแก้ที่เครื่องส่ง')


def check_pin(body):
    """The second secret. Unset = the whole feature is off: with saved keys, "start" alone would
    put a stage on the public channels for anyone holding the shared web login. No lockout on wrong
    tries: a lockout would let any editor keep the operator from pressing Stop. Guessing is made
    loud instead, and check_config insists on a PIN long enough that guessing is pointless."""
    if not RESTREAM_PIN:
        raise Forbidden('Restream ปิดอยู่: ตั้ง RESTREAM_PIN (12 ตัวขึ้นไป) ใน .env ของเซิร์ฟเวอร์ก่อน')
    if hmac.compare_digest(str(body.get('pin') or '').encode(), RESTREAM_PIN.encode()):
        return
    now = time.time()
    with RESTREAM_LOCK:
        PIN_FAILS[:] = [t for t in PIN_FAILS if now - t < 600] + [now]
        n = len(PIN_FAILS)
    log.warning('restream: wrong PIN (%d in the last 10 minutes)', n)
    if n >= 5:
        set_warning('rspin', f'มีการใส่ PIN ของ Restream ผิด {n} ครั้งใน 10 นาที (ล่าสุด {fmt_time(now)})')
    raise Forbidden('PIN ไม่ถูกต้อง')


def restream_file(stage):
    if not NAME_RE.match(stage or ''):
        raise BadRequest('ชื่อเวทีไม่ถูกต้อง')
    return RESTREAM / f'{stage}.json'


def load_dests(stage):
    try:
        return json.loads(restream_file(stage).read_text())
    except FileNotFoundError:
        return []   # a corrupt file raises instead: loud, never "no destinations"


def save_dests(stage, dests):
    RESTREAM.mkdir(parents=True, exist_ok=True)
    p = restream_file(stage)
    tmp = p.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds stream keys
    with os.fdopen(fd, 'w') as f:
        f.write(json.dumps(dests, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def set_dest(stage, did, **fields):
    """The file is the only copy of a destination: every change loads, edits one and saves, under
    the lock, so one destination's write can never put back another's older state."""
    with RESTREAM_LOCK:
        dests = load_dests(stage)
        for d in dests:
            if d['id'] == did:
                d.update(fields)
                save_dests(stage, dests)
                return d
    return None


def rs_event(stage, name, text):
    """One line per thing that happened to a destination: the page shows the latest, the file keeps
    them, so "Facebook was down this afternoon" has an answer after the fact."""
    line = {'at': time.time(), 'dest': name, 'text': text}
    with RESTREAM_LOCK:
        rs_log(stage)   # first use after a restart: read what the file already holds
        RS_LOG[stage].append(line)
        try:
            with (RESTREAM / f'{stage}.log').open('a') as f:
                f.write(json.dumps(line, ensure_ascii=False) + '\n')
        except OSError:
            log.exception('restream: could not write the event log of %s', stage)
    log.info('restream %s/%s: %s', stage, name, text)


def rs_log(stage):
    with RESTREAM_LOCK:
        if stage not in RS_LOG:
            RS_LOG[stage] = deque(maxlen=40)
            try:
                for raw in (RESTREAM / f'{stage}.log').read_text().splitlines()[-40:]:
                    RS_LOG[stage].append(json.loads(raw))
            except (OSError, ValueError):
                pass   # no log yet, or a torn last line: the live events still show
        return list(RS_LOG[stage])[-30:]


def dest_view(stage, d, now=None):
    """What the page gets: never the key (its last 4 characters tell two YouTube slots apart)."""
    now = now or time.time()
    key = d.get('key') or ''
    expire = re.search(r'[?&]expire=(\d{10})(?:&|$)', key)
    v = {'id': d['id'], 'name': d.get('name'), 'server': d.get('server'), 'has_key': bool(key),
         'key_tail': key[-4:] if len(key) >= 12 else '', 'key_saved_at': d.get('key_saved_at'),
         'key_expires': int(expire[1]) if expire else None, 'armed': bool(d.get('armed')),
         'status': 'off', 'text': None, 'note': None, 'since': None, 'kbps': None, 'retries': 0,
         'air': 0, 'at': None, 'by': None, 'idle': None, 'down_since': None}
    run = RESTREAM_RUN.get((stage, d['id']))
    if run:
        status, text = run['status'], run['text']
        idle = now - run['advance_at'] if run['advance_at'] else None
        if status == 'streaming' and idle is not None and idle > RESTREAM_FRESH:
            # derived when read, so a wrong transition cannot keep a dead push looking alive
            status, text = 'stalled', f'ไม่มีข้อมูลออกไปปลายทางมา {idle:.0f} วินาทีแล้ว'
        note = run['note']
        if run['via'] == 'rtsp':
            note = RTSP_NOTE + ((' · ' + note) if note else '')
        if run['ts_warn'] > 25:
            note = ((note + ' · ') if note else '') + (f'timestamp ของเครื่องส่งไม่เรียง {run["ts_warn"]} ครั้งในการเชื่อมต่อนี้ '
                                                       'ภาพที่ปลายทางอาจกระตุก')
        v.update(status=status, text=text, note=note, retries=run['retries'], down_since=run['down_since'],
                 since=run['advance_at'] if status == 'stalled' else run['since'],
                 kbps=run['kbps'] if status == 'streaming' else None,
                 air=round(run['air'] + run['sent'], 1), idle=round(idle, 1) if idle is not None else None)
    elif d.get('armed'):
        # the file says armed but no thread is: its end could not be written (disk full)
        v.update(status='failed', by='system',
                 text='ตัวส่งหยุดไปโดยไม่ได้บันทึกสาเหตุ ดู log ของ web แล้วกดเริ่มใหม่')
    elif d.get('last'):
        last = d['last']
        v.update(status=last.get('status') or 'off', text=last.get('text'), at=last.get('at'),
                 air=last.get('air') or 0, by=last.get('by'))
    return v


def restream_state(names):
    now = time.time()
    stages, bad, warn = {}, {}, {}
    files = {f.stem for f in RESTREAM.glob('*.json')} if RESTREAM.exists() else set()
    for stage in sorted(set(names) | files):
        if not NAME_RE.match(stage):
            continue
        try:
            with RESTREAM_LOCK:
                views = [dest_view(stage, d, now) for d in load_dests(stage)]
            error = None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            # never "no destinations": say so, and keep the rows (and Stop buttons) of what is still running
            error = bad[f'restream:{stage}'] = f'อ่านรายการปลายทาง Restream ของ {stage} ไม่ได้: {type(e).__name__}: {e}'
            with RESTREAM_LOCK:
                views = [dest_view(stage, {'id': r['id'], 'name': r['name'], 'armed': True}, now)
                         for (st, _), r in list(RESTREAM_RUN.items()) if st == stage]
        stages[stage] = {'dests': views, 'log': rs_log(stage), 'error': error,
                         'hook': youtube_state(YOUTUBE, stage, now) is not None}
        for v in views:
            # the yellow bar on every editor's page: nobody watches this card during a show
            head = f'Restream {stage} → {v["name"]}: '
            age = now - v['since'] if v['since'] else 0
            if v['status'] == 'failed' or (v['status'] == 'stopped' and v['by'] == 'system'):
                if now - (v['at'] or now) < 12 * 3600:
                    warn[f'rs:{stage}:{v["id"]}'] = head + ('ล้มเหลว ' if v['status'] == 'failed' else 'ระบบหยุดส่งเอง ') + (v['text'] or '')
            elif (v['down_since'] and now - v['down_since'] > RESTREAM_WARN_AFTER
                  and v['status'] in ('retrying', 'connecting', 'streaming')):
                # "since" restarts on every reconnect, so a flapping channel would never get here by its age
                warn[f'rs:{stage}:{v["id"]}'] = head + f'ส่งไม่นิ่งมาตั้งแต่ {fmt_time(v["down_since"])} ' + (v['text'] or '')
            elif v['status'] == 'stalled' or (v['status'] == 'waiting' and age > 60):
                warn[f'rs:{stage}:{v["id"]}'] = head + (v['text'] or v['status'])
    sync_warnings('restream:', bad)
    sync_warnings('rs:', warn)
    with RESTREAM_LOCK:
        if PIN_FAILS and now - PIN_FAILS[-1] > 600:
            PIN_FAILS.clear()
            set_warning('rspin', None)
        running = len(RESTREAM_RUN)
    return {'enabled': bool(RESTREAM_PIN), 'max': RESTREAM_MAX, 'per_stage': RESTREAM_PER_STAGE,
            'window_min': RECONNECT_MIN, 'running': running, 'stages': stages}


def save_restream_dest(stage, body):
    name = re.sub(r'[\x00-\x1f\x7f]', '', str(body.get('name') or '')).strip()[:60]
    if not name:
        raise BadRequest('ตั้งชื่อปลายทาง เช่น YouTube public')
    did = body.get('id') or None
    server = str(body.get('server') or '').strip()
    new_key = str(body.get('key') or '').strip()
    with RESTREAM_LOCK:
        cur = next((d for d in load_dests(stage) if d['id'] == did), None) if did else None
    if did and not cur:
        raise BadRequest('ไม่พบปลายทางนี้ อาจถูกลบไปแล้ว')
    if cur and not new_key and server.rstrip('/') != str(cur.get('server') or '').rstrip('/'):
        # ffmpeg sends the key in the clear to whatever host "server" names: keeping the saved key
        # while pointing the destination elsewhere would hand the key to that host
        raise BadRequest('เปลี่ยน Server ต้องใส่ Stream key ใหม่ด้วย')
    key = new_key or (cur or {}).get('key', '')
    rtmp_dest(server, key)   # refuses a bad server/key before anything is saved (DNS: outside the lock)
    with RESTREAM_LOCK:
        dests = load_dests(stage)
        rec = next((d for d in dests if d['id'] == did), None) if did else None
        if did and not rec:
            raise BadRequest('ไม่พบปลายทางนี้ อาจถูกลบไปแล้ว')
        if rec and (stage, did) in RESTREAM_RUN:
            raise ClipError('ปลายทางนี้กำลังส่งอยู่ หยุดก่อนแล้วค่อยแก้')
        if not rec:
            if len(dests) >= RESTREAM_PER_STAGE:
                raise ClipError(f'เวทีหนึ่งมีปลายทางได้ไม่เกิน {RESTREAM_PER_STAGE} ช่อง ลบอันที่ไม่ใช้ก่อน')
            rec = {'id': uuid.uuid4().hex, 'armed': False, 'armed_at': None, 'proven': False, 'last': None}
            dests.append(rec)
        if new_key or 'key_saved_at' not in rec:
            rec['key_saved_at'] = time.time()
        rec.update(name=name, server=server, key=key)
        save_dests(stage, dests)
        rs_event(stage, name, 'บันทึกปลายทาง' + (' พร้อม key ใหม่' if new_key else ''))
        return dest_view(stage, rec)


def delete_restream_dest(stage, did):
    with RESTREAM_LOCK:
        if (stage, did) in RESTREAM_RUN:
            raise ClipError('ปลายทางนี้กำลังส่งอยู่ หยุดก่อนแล้วค่อยลบ')
        dests = load_dests(stage)
        gone = [d for d in dests if d['id'] == did]
        if not gone:
            raise BadRequest('ไม่พบปลายทางนี้ อาจถูกลบไปแล้ว')
        save_dests(stage, [d for d in dests if d['id'] != did])
        rs_event(stage, gone[0].get('name'), 'ลบปลายทาง')


def sample_stages():
    """Which stages deliver: path there AND its byte counter moved since the last look. "Ready"
    alone lies: an encoder can hold the connection and send nothing (20 minutes on 4 Nov 2026).
    Three answers, never two: True, False, or None when MediaMTX cannot be asked or does not say -
    unknown starts nothing, stops nothing, and is never worded as "no signal"."""
    paths = mediamtx_paths()
    now = time.time()
    with RESTREAM_LOCK:
        FLOW_ASKED[0] = paths is not None
        if paths is None:
            for flow in STAGE_FLOW.values():
                flow.update(ready=None, flowing=None)
            return
        for name in set(STAGE_FLOW) - set(paths):
            STAGE_FLOW[name] = {'ready': False, 'flowing': False, 'bytes': None, 'tracks': None, 't': now}
        for name, item in paths.items():
            prev = STAGE_FLOW.get(name) or {}
            if prev.get('t') and now - prev['t'] < RESTREAM_MIN_GAP and prev.get('ready') is not None:
                continue
            ready = item.get('available', item.get('ready'))       # v1.21 renamed the fields
            got = item.get('inboundBytes', item.get('bytesReceived'))
            if ready is None or (ready and got is None):
                flowing = None
            else:
                flowing = None if prev.get('bytes') is None else bool(ready) and got > prev['bytes']
                if not ready:
                    flowing = False
            STAGE_FLOW[name] = {'ready': None if ready is None else bool(ready), 'flowing': flowing,
                                'bytes': got if ready else None, 'tracks': item.get('tracks'), 't': now}


def stage_flow(stage):
    with RESTREAM_LOCK:
        if stage in STAGE_FLOW:
            return dict(STAGE_FLOW[stage])
        # not listed by MediaMTX = no signal, but only if MediaMTX has actually been asked
        known = False if FLOW_ASKED[0] else None
        return {'ready': known, 'flowing': known, 'bytes': None, 'tracks': None}


def restream_input(stage, via='srt'):
    """ffmpeg input arguments for reading a stage back from MediaMTX (same login as the API)."""
    user, pw = quote(MEDIAMTX_API_USER, safe=''), quote(MEDIAMTX_API_PASS, safe='')
    host = urlparse(MEDIAMTX_API).hostname
    if via == 'rtsp':
        src = f'rtsp://{user}:{pw}@{host}:8554/{stage}'
    elif RESTREAM_SOURCE:
        src = RESTREAM_SOURCE.replace('{stage}', stage)
    else:
        src = f'srt://{host}:8890?streamid=read:{stage}:{MEDIAMTX_API_USER}:{MEDIAMTX_API_PASS}'
    # -timeout is the socket timeout only for RTSP (for rtmp it would mean "listen"); elsewhere rw_timeout
    opts = ['-rtsp_transport', 'tcp', '-timeout', '10000000'] if src.startswith('rtsp') else ['-rw_timeout', '10000000']
    return [*opts, '-i', src]


def new_run(stage, d, dest):
    return {'stage': stage, 'id': d['id'], 'name': d.get('name'), 'dest': dest, 'proven': bool(d.get('proven')),
            'stop': threading.Event(), 'reason': None, 'proc': None,
            'status': 'connecting', 'text': None, 'note': None, 'since': time.time(), 'kbps': None,
            'retries': 0, 'air': 0.0, 'sent': 0.0, 'bytes': 0, 'spawned': None, 'advance_at': None,
            'rate': None, 'lag': None, 'killed': None, 'via': 'srt', 'ts_warn': 0, 'down_since': None}


def rs_status(run, status, text=None):
    """Change a running destination's status; the time and the event log move only on a real change."""
    if run['status'] != status:
        run['since'] = time.time()
        if status != 'connecting':
            rs_event(run['stage'], run['name'], {'streaming': 'ปลายทางรับสัญญาณแล้ว', 'retrying': 'ช่องนี้หลุด: ',
                                                 'waiting': 'รอเวที: ', 'stopping': 'กำลังหยุด'}.get(status, status)
                     + ((text or '') if status in ('retrying', 'waiting') else ''))
    run.update(status=status, text=text)


def run_push(stage, did):
    run = RESTREAM_RUN[(stage, did)]
    name = run['name']
    proven, fails, fail_since, away_since = run['proven'], 0, None, None
    read_ends = deque(maxlen=RESTREAM_READ_ENDS)   # when the READ of the stage ended although the stage was fine
    final = ('failed', 'ตัวส่งหยุดโดยไม่ทราบสาเหตุ ดู log ของ web', 'system')
    secrets = (run['dest']['key'], run['dest']['key'].split('?')[0], MEDIAMTX_API_PASS)
    try:
        while not run['stop'].is_set():
            flow, now = stage_flow(stage), time.time()
            if flow['flowing'] is not True:
                # the stage is not delivering: wait for it like dvr.sh waits to continue a round
                away_since = away_since or now
                fails, fail_since = 0, None     # whatever the destination was doing, it starts clean
                run['down_since'] = None
                why = ('ติดต่อ MediaMTX ไม่ได้ ยังไม่รู้ว่าเวทีมีสัญญาณไหม' if flow['ready'] is None
                       else 'เวทีไม่มีสัญญาณเข้า' if not flow['ready']
                       else 'กำลังดูว่าเวทีมีภาพเข้าหรือไม่' if flow['flowing'] is None
                       else 'เครื่องส่งของเวทีต่ออยู่แต่ไม่มีภาพเข้า')
                if now - away_since > RECONNECT_MIN * 60:
                    final = ('stopped', f'{why}เกิน {RECONNECT_MIN:g} นาที ระบบหยุดส่งและปลดช่องนี้ให้เอง '
                                        'ถ้ายังต้องออกอากาศ กดเริ่มส่งใหม่เมื่อเวทีกลับมา', 'system')
                    return
                until = fmt_time(away_since + RECONNECT_MIN * 60)[:5]
                rs_status(run, 'waiting', f'{why} จะส่งต่อเองเมื่อภาพกลับมา ถ้าไม่กลับภายใน {until} จะหยุดเอง')
                run['stop'].wait(1)
                continue
            if away_since and now - away_since > 60:
                gap = now - away_since
                run['note'] = (f'ส่งต่อเมื่อ {fmt_time(now)[:5]} หลังเวทีขาดไป {gap // 60:.0f}:{gap % 60:02.0f} นาที '
                               'ตรวจที่แพลตฟอร์มว่าไลฟ์เดิมยังเปิดอยู่')
                rs_event(stage, name, run['note'])
            away_since = None
            run.update(spawned=time.time(), advance_at=None, rate=None, lag=None, killed=None, sent=0.0, bytes=0,
                       kbps=None, ts_warn=0)
            rs_status(run, 'connecting', run['text'] if run['status'] == 'retrying' else None)
            tail = deque(maxlen=8)    # the cause is several lines up: the last ones only say "I/O error"
            rc = None
            try:
                # checked before every connection, not only when saved: the name must still be public
                d = next((x for x in load_dests(stage) if x['id'] == did), None)
                if not d:
                    final = ('stopped', 'ปลายทางนี้ถูกลบไปแล้ว', 'system')
                    return
                dest = run['dest'] = rtmp_dest(d.get('server'), d.get('key'))
            except BadRequest as e:
                tail.append(str(e))
            else:
                # one video and one audio track: a second one would go out as multitrack FLV, which the
                # platforms do not take, and a track that never delivers would hold "sending" back for good
                cmd = REPLAY_FFMPEG + ['-nostdin', '-hide_banner', '-loglevel', 'warning', '-progress', 'pipe:2',
                                       *restream_input(stage, run['via']),
                                       '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', *rtmp_out_args(dest)]
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True, errors='replace')
                run['proc'] = proc
                if run['stop'].is_set():  # stop pressed while this ffmpeg was starting
                    proc.terminate()
                for line in proc.stderr:
                    line = line.strip()
                    if PROGRESS_RE.match(line):
                        k, _, v = line.partition('=')
                        now = time.time()
                        if k == 'total_size' and v.isdigit():
                            size = int(v)
                            # ffmpeg prints this block twice a second whether or not anything moves:
                            # only GROWING bytes mean something is leaving
                            if size > run['bytes']:
                                run.update(bytes=size, advance_at=now)
                            t0, size0 = run['rate'] or (now, size)
                            if run['rate'] is None:
                                run['rate'] = (now, size)
                            elif now - t0 >= 2:
                                run.update(kbps=round((size - size0) * 8 / 1000 / (now - t0)), rate=(now, size))
                        elif k == 'out_time_us' and v.isdigit() and int(v):
                            if run['status'] != 'streaming' and run['status'] != 'stopping':
                                # a position only appears after the output opened: the platform took
                                # the publish and picture is leaving. Never "sending" on a hope.
                                rs_status(run, 'streaming')
                            run['sent'] = int(v) / 1e6
                            if run['sent'] >= REPLAY_STEADY and (not proven or run['down_since']):
                                # it works: said now, not when the attempt ends - a show that ends
                                # because the stage dropped, or an app restart, must not lose this
                                run['down_since'] = None
                                if not proven:
                                    proven = True
                                    try:
                                        set_dest(stage, did, proven=True)
                                    except (OSError, ValueError):
                                        log.exception('restream %s/%s: could not save "proven"', stage, did[:8])
                        continue
                    line = redact(line, *secrets)
                    if 'Invalid DTS' in line or 'Non-monotonic DTS' in line or 'Invalid timestamps' in line:
                        # a messy encoder: ffmpeg repairs one frame's timestamp and says so for every frame.
                        # Counted, not logged one by one, and kept out of the tail that explains a failure.
                        run['ts_warn'] += 1
                        if run['ts_warn'] in (1, 100, 1000) or run['ts_warn'] % 10000 == 0:
                            log.warning('restream %s/%s: encoder timestamps out of order, %d so far in this connection (%s)',
                                        stage, did[:8], run['ts_warn'], line[-160:])
                        continue
                    if line:
                        log.info('restream %s/%s: %s', stage, did[:8], line[-300:])
                        if not REPEATED_RE.match(line):
                            tail.append(tidy(line)[-200:])
                rc = proc.wait()
            sent, killed = run['sent'], run['killed']
            run.update(proc=None, kbps=None, air=run['air'] + sent, sent=0.0, advance_at=None)
            if sent >= REPLAY_STEADY:
                fails, fail_since = 0, None   # a good spell: whatever follows is a new episode
            if run['stop'].is_set():
                break
            sample_stages()   # a fresh look: was the stage still delivering when this ended?
            if stage_flow(stage)['flowing'] is not True:
                # the stage ended, went silent, or cannot be asked: not this destination's doing.
                # The top of the loop waits for the picture and gives up after RECONNECT_MIN.
                run['stop'].wait(1)
                continue
            # the stage delivers, so this attempt failed: the destination, or our read of the stage
            fails += 1
            fail_since = fail_since or time.time()
            run['down_since'] = run['down_since'] or fail_since   # the yellow bar counts from here
            # ffmpeg ends with 0 when its INPUT ended (it swallows demux errors) and non-zero when the
            # output failed; an input that could not even be opened says so
            cant_open = any('Error opening input' in t for t in tail)
            input_side = not killed and (rc == 0 or cant_open)
            lines = sorted(tail, key=lambda t: 'Server error' not in t)   # the platform's own reason first
            err = ({'stall': f'ไม่มีข้อมูลออก {RESTREAM_STALL:g} วินาทีทั้งที่เวทีมีสัญญาณ',
                    'connect': f'ภาพแรกไม่ออกภายใน {RESTREAM_CONNECT:g} วินาที',
                    'behind': 'ปลายทางรับไม่ทันเวลาจริง (เน็ตขาออกไม่พอ หรือปลายทางช้า) ตัดแล้วต่อใหม่เพื่อไม่ให้ภาพเสีย'}.get(killed)
                   or ' / '.join(lines[:4]) or f'ffmpeg จบด้วยรหัส {rc}')
            if input_side:
                err = ('อ่านสัญญาณเวทีจาก MediaMTX ไม่ได้ ไม่เกี่ยวกับ Stream key (ตรวจสิทธิ์ read ของผู้ใช้ liveclip'
                       f'{" และ rtsp: yes" if run["via"] == "rtsp" else ""} ใน mediamtx.yml): {err}' if cant_open
                       else 'การอ่านสัญญาณเวทีหลุดทั้งที่เวทียังส่งอยู่')
                read_ends.append(time.time())
                if (run['via'] == 'srt' and not RESTREAM_SOURCE and not cant_open and len(read_ends) == RESTREAM_READ_ENDS
                        and read_ends[-1] - read_ends[0] < 60):
                    # MediaMTX keeps dropping the SRT reader: this encoder's timestamps are messy.
                    # RTSP survives that, at the price of audio arriving a little late.
                    run['via'] = 'rtsp'   # dest_view shows RTSP_NOTE for as long as this holds
                    rs_event(stage, name, RTSP_NOTE)
            if not proven and fails >= 2 and (not input_side or cant_open):
                hint = '' if input_side else 'ส่งไม่ขึ้นตั้งแต่แรก: ตรวจ Server กับ Stream key และว่าปลายทางเปิดรับไลฟ์แล้ว '
                final = ('failed', f'{hint}({err})' if hint else err, 'system')
                return
            if time.time() - fail_since > RESTREAM_GIVEUP:
                final = ('failed', f'ส่งให้นิ่งเกิน {REPLAY_STEADY:g} วินาทีไม่ได้เลยมา {RESTREAM_GIVEUP // 60:g} นาที '
                                   f'มีเครื่องอื่นส่งเข้า key นี้อยู่หรือไม่ ({err})', 'system')
                return
            run['retries'] += 1
            rs_status(run, 'retrying', f'{err} (หลุดครั้งที่ {fails} กำลังต่อใหม่)')
            run['stop'].wait(RESTREAM_BACKOFF[min(fails, len(RESTREAM_BACKOFF)) - 1])
        final = ('stopped', run['reason'] or 'มีคนกดหยุด', 'user' if run['reason'] is None else 'system')
    except Exception as e:  # noqa: BLE001 - recorded on the destination, never left "sending"
        log.exception('restream %s/%s crashed', stage, did[:8])
        final = ('failed', redact(f'{type(e).__name__}: {e}', *secrets), 'system')
    finally:
        proc = run.get('proc')
        if proc and proc.poll() is None:
            proc.kill()
        text = final[1] + (' · ไลฟ์ที่แพลตฟอร์มยังไม่จบจนกว่าจะกดจบที่นั่น' if final[0] == 'stopped' and final[2] == 'user' else '')
        with RESTREAM_LOCK:
            RESTREAM_RUN.pop((stage, did), None)
            rs_event(stage, name, {'stopped': 'หยุดส่งแล้ว: ', 'failed': 'ล้มเหลว: '}[final[0]] + final[1])
            try:
                set_dest(stage, did, armed=False, last={'status': final[0], 'text': text, 'at': time.time(),
                                                        'air': round(run['air'], 1), 'by': final[2]})
            except (OSError, ValueError):
                # dest_view shows an armed destination without a thread as failed, so this is not silent
                log.exception('restream %s/%s: could not record that it ended (%s)', stage, did[:8], final[0])


def start_restream(stage, ids):
    """[{id, ok, error}] per destination: one bad destination never holds back the others, and a
    refusal is written on the destination so the next poll still shows it."""
    now = time.time()
    if youtube_state(YOUTUBE, stage, now) is not None:
        raise ClipError(f'เวที {stage} ยังมี key ของ YouTube push แบบเก่าค้างอยู่ (ปิดด้วย cloud/set-youtube-key.sh <IP ของ VM> {stage} --off) '
                        'ปิดก่อน ไม่อย่างนั้นจะมีสองตัวส่งพร้อมกันโดยที่การ์ดนี้มองไม่เห็น')
    sample_stages()
    time.sleep(RESTREAM_SAMPLE_GAP)
    sample_stages()
    flow = stage_flow(stage)
    if flow['ready'] is None or flow['flowing'] is None:
        raise ClipError('ติดต่อ MediaMTX ไม่ได้ จึงยังไม่รู้ว่าเวทีมีสัญญาณหรือไม่ ลองใหม่อีกครั้ง')
    if not flow['ready']:
        raise ClipError(f'เวที {stage} ไม่มีสัญญาณเข้า เริ่มส่งได้เมื่อเวทีมีสัญญาณแล้ว')
    if not flow['flowing']:
        raise ClipError(f'เครื่องส่งของเวที {stage} ต่ออยู่แต่ไม่มีภาพเข้ามา ตรวจเครื่องส่งก่อน')
    tracks = flow.get('tracks')
    if isinstance(tracks, list) and tracks and not ('H264' in tracks and 'MPEG-4 Audio' in tracks):
        raise ClipError(f'เวที {stage} ส่งมาเป็น {", ".join(map(str, tracks))} แพลตฟอร์มต้องการวิดีโอ H.264 พร้อมเสียง AAC')
    out = []
    for did in ids:
        try:
            out.append({'id': did, 'ok': True, 'note': start_one(stage, did)})
        except (ClipError, BadRequest) as e:
            out.append({'id': did, 'ok': False, 'error': str(e)})
            with RESTREAM_LOCK:
                if (stage, did) not in RESTREAM_RUN:
                    d = set_dest(stage, did, last={'status': 'failed', 'text': f'ไม่ได้เริ่มส่ง: {e}', 'at': time.time(),
                                                   'air': 0, 'by': 'system'})
                    if d:
                        rs_event(stage, d.get('name'), f'ไม่ได้เริ่มส่ง: {e}')
    return out


def start_one(stage, did):
    def running():
        run = RESTREAM_RUN.get((stage, did))
        if run and run['stop'].is_set():
            raise ClipError('ช่องนี้กำลังหยุดอยู่ รอให้หยุดเสร็จแล้วกดเริ่มส่งใหม่')
        return run
    with RESTREAM_LOCK:
        d = next((x for x in load_dests(stage) if x['id'] == did), None)
        if d and running():
            return 'ส่งอยู่แล้ว'
    if not d:
        raise BadRequest('ไม่พบปลายทางนี้ อาจถูกลบไปแล้ว')
    dest = rtmp_dest(d.get('server'), d.get('key'))
    # REPLAY_LOCK inside RESTREAM_LOCK, held to the end: create_replay takes only REPLAY_LOCK for its
    # own admission, so a restream and a replay can never both pass the "key in use" and uplink checks
    with RESTREAM_LOCK, REPLAY_LOCK:
        if running():
            return 'ส่งอยู่แล้ว'
        replays = len(REPLAY_RUN)
        if len(RESTREAM_RUN) + replays >= RESTREAM_MAX:
            also = f' (นับ Replay ที่กำลังส่ง {replays} ตัวด้วย หยุด Replay ก่อน)' if replays else ''
            raise ClipError(f'ส่งพร้อมกันได้ไม่เกิน {RESTREAM_MAX} ช่องรวมทุกเวที เพราะใช้เน็ตขาออกเส้นเดียวกัน{also}')
        if dest_in_use(dest):
            raise ClipError('Server + Stream key นี้กำลังถูกใช้ส่งอยู่ (อีกช่องหนึ่ง หรือ Replay) สองตัวบนคีย์เดียวกันจะเตะกันเอง')
        d = set_dest(stage, did, armed=True, armed_at=time.time(), proven=False, last=None)
        if not d:
            raise BadRequest('ไม่พบปลายทางนี้ อาจถูกลบไปแล้ว')
        RESTREAM_RUN[(stage, did)] = new_run(stage, d, dest)
        rs_event(stage, d.get('name'), 'กดเริ่มส่ง')
        try:
            threading.Thread(target=run_push, args=(stage, did), daemon=True,
                             name=f'restream-{stage}-{did[:8]}').start()
        except BaseException:
            RESTREAM_RUN.pop((stage, did), None)
            set_dest(stage, did, armed=False, last={'status': 'failed', 'text': 'เริ่มตัวส่งไม่ได้ ดู log ของ web',
                                                    'at': time.time(), 'air': 0, 'by': 'system'})
            raise
    return None


def stop_restream(stage, ids, reason=None):
    out = []
    for did in ids:
        with RESTREAM_LOCK:
            run = RESTREAM_RUN.get((stage, did))
            if not run:
                out.append({'id': did, 'ok': False, 'error': 'ช่องนี้ไม่ได้ส่งอยู่แล้ว'})
                continue
            try:
                # disarm on disk BEFORE signalling: a restart in the next seconds must not bring it back
                set_dest(stage, did, armed=False)
            except (OSError, ValueError) as e:
                log.exception('restream %s/%s: could not save the stop', stage, did[:8])
                out.append({'id': did, 'ok': True, 'error': f'สั่งหยุดแล้ว แต่บันทึกลงดิสก์ไม่ได้ ({type(e).__name__}) '
                                                             'ถ้าแอปรีสตาร์ท ช่องนี้อาจกลับมาส่งเอง ตรวจพื้นที่ดิสก์'})
            else:
                out.append({'id': did, 'ok': True})
            run['reason'] = reason
            run['stop'].set()
            rs_status(run, 'stopping', 'กำลังหยุด รอ ffmpeg ปิดการเชื่อมต่อ')
            proc = run.get('proc')
        if proc and proc.poll() is None:
            proc.terminate()  # ffmpeg closes the RTMP stream cleanly: the platform sees it end
            # one SIGTERM does not break a blocked RTSP read: this kill is what ends a silent stage
            threading.Timer(15, lambda p=proc: p.poll() is None and p.kill()).start()
    return out


def restream_watch_once(now=None):
    """Every RESTREAM_TICK while anything is armed: refresh which stages deliver, and kill an
    ffmpeg from which nothing leaves (a stuck process must end loudly, not look like "sending")."""
    with RESTREAM_LOCK:
        runs = list(RESTREAM_RUN.values())
    if not runs:
        return
    sample_stages()
    now = now or time.time()
    for run in runs:
        proc = run.get('proc')
        if not proc or proc.poll() is not None or run['killed'] or run['status'] == 'stopping':
            continue
        if run['status'] != 'streaming':
            # whatever the byte counter did (the FLV header alone moves it): no picture left in time
            if run['spawned'] and now - run['spawned'] > RESTREAM_CONNECT:
                run['killed'] = 'connect'
                proc.kill()
        elif now - (run['advance_at'] or run['since']) > RESTREAM_STALL:
            run['killed'] = 'stall'
            proc.kill()
        else:
            # Bytes move, but does the push keep up? A destination (or uplink) slower than real time
            # makes ffmpeg read the stage late, MediaMTX then discards packets for it and the picture
            # at the platform breaks up while everything here looks fine. Media seconds sent are
            # compared with the clock over a window; falling behind = cut and reconnect at the live edge.
            ref = run['lag']
            if ref is None:
                run['lag'] = (now, run['sent'])
            elif now - ref[0] >= RESTREAM_LAG_WINDOW:
                if (now - ref[0]) - (run['sent'] - ref[1]) > RESTREAM_LAG_MAX and stage_flow(run['stage'])['flowing']:
                    run['killed'] = 'behind'
                    proc.kill()
                else:
                    run['lag'] = (now, run['sent'])
    try:
        (RESTREAM / '.alive').touch()   # resume_restreams: how long was the app away?
    except OSError:
        log.exception('restream: could not touch the heartbeat file (a restart would not resume)')


def restream_watch_loop():
    while True:
        try:
            restream_watch_once()
            set_warning('rswatch', None)
        except Exception as e:  # noqa: BLE001 - keep looping, but say so on the page
            log.exception('restream watch failed')
            set_warning('rswatch', f'ตัวเฝ้า Restream ล้มเหลว (ช่องที่ค้างจะไม่ถูกตัดให้เอง): {type(e).__name__}: {e}')
        time.sleep(RESTREAM_TICK)


def repair_restreams(now=None):
    """An armed destination with no thread is one whose end could not be written (disk full). Left
    alone it would stay "armed" for ever and a restart would push it again: write the end now."""
    now = now or time.time()
    for f in sorted(RESTREAM.glob('*.json')) if RESTREAM.exists() else []:
        stage = f.stem
        if not NAME_RE.match(stage):
            continue
        with RESTREAM_LOCK:
            try:
                for d in load_dests(stage):
                    if d.get('armed') and (stage, d['id']) not in RESTREAM_RUN:
                        set_dest(stage, d['id'], armed=False, last={
                            'status': 'failed', 'at': now, 'air': 0, 'by': 'system',
                            'text': 'ตัวส่งหยุดไปโดยไม่ได้บันทึกสาเหตุ ดู log ของ web แล้วกดเริ่มใหม่'})
                        rs_event(stage, d.get('name'), 'พบช่องที่ค้างสถานะกำลังส่งโดยไม่มีตัวส่ง ปลดให้แล้ว')
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                log.exception('restream: could not repair the destinations of %s', stage)


def resume_restreams():
    """After a restart, carry on what was being sent - but only if the app was away for less than
    the reconnect window and the PIN is still set. A server that was off overnight must not put
    tomorrow's rehearsal on the public channels with yesterday's keys."""
    alive = RESTREAM / '.alive'
    away = time.time() - alive.stat().st_mtime if alive.exists() else None
    fresh = bool(RESTREAM_PIN) and away is not None and away <= RECONNECT_MIN * 60
    sample_stages()   # so a resumed push starts from what MediaMTX says, not from "never asked"
    for f in sorted(RESTREAM.glob('*.json')):
        stage = f.stem
        if not NAME_RE.match(stage):
            continue
        try:
            armed = [d for d in load_dests(stage) if d.get('armed')]
            for d in armed:
                did, text = d['id'], None
                if not fresh:
                    text = ('แอปเริ่มใหม่โดยไม่มี RESTREAM_PIN จึงไม่ส่งต่อให้เอง' if not RESTREAM_PIN else
                            f'แอปหยุดไป {away / 60:.0f} นาที เกินช่วงต่อกลับ จึงไม่ส่งต่อให้เอง กดเริ่มใหม่เมื่อพร้อม' if away is not None
                            else 'ไม่รู้ว่าแอปหยุดไปนานเท่าไร จึงไม่ส่งต่อให้เอง กดเริ่มใหม่เมื่อพร้อม')
                else:
                    try:
                        # no lookup here: the network may not be up yet in the first second after a
                        # restart, and run_push checks the name before every connection anyway
                        dest = rtmp_dest(d.get('server'), d.get('key'), resolve=False)
                    except BadRequest as e:
                        text = f'ส่งต่อหลังแอปเริ่มใหม่ไม่ได้: {e}'
                if text:
                    set_dest(stage, did, armed=False, last={'status': 'stopped', 'text': text, 'at': time.time(),
                                                            'air': 0, 'by': 'system'})
                    rs_event(stage, d.get('name'), text)
                    continue
                with RESTREAM_LOCK:
                    RESTREAM_RUN[(stage, did)] = new_run(stage, d, dest)
                rs_event(stage, d.get('name'), f'แอปเริ่มใหม่ (หยุดไป {away:.0f} วินาที) ส่งต่อให้เอง')
                threading.Thread(target=run_push, args=(stage, did), daemon=True,
                                 name=f'restream-{stage}-{did[:8]}').start()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, BadRequest) as e:
            # a bad or unwritable file is a warning on the page; it must not stop the app from starting
            log.exception('restream: could not resume %s', stage)
            set_warning(f'restream:{stage}', f'อ่านหรือเขียนรายการปลายทาง Restream ของ {stage} ไม่ได้: {type(e).__name__}: {e}')


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
        try:
            if (c.get('status') == 'done' and c.get('s3_key') and p.exists()
                    and now - c.get('created', now) > LOCAL_CLIP_HOURS * 3600):
                p.unlink(missing_ok=True)
                update_clip(c, local=False)
            # A job that is "in progress" but not in any pool died without recording its result.
            if c.get('status') in IN_PROGRESS and cid not in running and CLIP_ID_RE.match(cid):
                jp = clip_path(cid, 'json')
                if jp.exists() and now - jp.stat().st_mtime > 120:
                    update_clip(c, status='failed', failed_stage=stage_after_interruption(c),
                                error='งานหยุดกลางคันโดยไม่ได้บันทึกผล กด "ลองใหม่"')
        except (ClipGone, FileNotFoundError):
            continue  # someone deleted it between the listing and here
    for t in CLIPS.glob('tmp-*'):
        if t.name[4:] not in running and now - t.stat().st_mtime > 6 * 3600:
            shutil.rmtree(t, ignore_errors=True)
    janitor_replays(now)
    repair_restreams(now)

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

    def parse_request(self):
        ok = super().parse_request()
        # A request that declares a body we may never read (401, 415, any GET) must not leave it on
        # a kept-alive connection: it would be parsed as the next request, and behind a proxy that
        # pools connections the next request is someone else's, with their login.
        if ok and ((self.headers.get('Content-Length') or '0').strip() != '0'
                   or self.headers.get('Transfer-Encoding')):
            self.close_connection = True
        return ok

    def end_headers(self):
        if self.close_connection:
            self.send_header('Connection', 'close')  # the proxy must not put this connection back in its pool
        super().end_headers()

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
        f = None
        if clip.get('local'):
            try:  # opened before any header: a clip deleted right now is a 404, not a 200 that breaks off
                f = clip_path(cid, 'mp4').open('rb')
            except FileNotFoundError:
                pass
        if f:
            with f:
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(os.fstat(f.fileno()).st_size))
                self.send_header('Content-Disposition', content_disposition(clip['name']))
                self.end_headers()
                if self.command != 'HEAD':
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
            if len(parts) == 4 and parts[:2] == ['api', 'clips'] and parts[3] == 'delete':
                return self.json(delete_clip(parts[2], body.get('status')))
            if path == '/api/replays':
                return self.json(create_replay(body), 201)
            if len(parts) == 4 and parts[:2] == ['api', 'replays'] and parts[3] == 'stop':
                if not CLIP_ID_RE.match(parts[2]):
                    raise BadRequest('รหัส replay ไม่ถูกต้อง')
                return self.json(stop_replay(parts[2]))
            if len(parts) >= 4 and parts[:2] == ['api', 'restream']:
                return self.json(self.restream(parts[2], parts[3:], body))
            if path == '/api/settings':
                for key in DEFAULTS:
                    if key in body:
                        set_setting(key, body[key])
                return self.json({k: get_setting(k) for k in DEFAULTS})
            self.json({'error': 'not found'}, 404)
        except Forbidden as e:
            self.json({'error': str(e), 'pin': True}, 403)
        except OSError as e:
            log.exception('POST %s could not write', self.path.split('?')[0])
            self.json({'error': f'เขียนไฟล์บนเซิร์ฟเวอร์ไม่ได้ ({type(e).__name__}) คำสั่งนี้อาจยังไม่ได้ทำ ตรวจพื้นที่ดิสก์'}, 500)
        except ClipError as e:
            self.json({'error': str(e)}, 409)
        except (BadRequest, ValueError) as e:
            self.json({'error': str(e)}, 400)

    def restream(self, stage, rest, body):
        check_pin(body)   # every restream action, before anything else
        restream_file(stage)   # refuses an odd stage name
        ids = body.get('ids')
        if rest in (['start'], ['stop']):
            if not isinstance(ids, list) or not 0 < len(ids) <= 8 or not all(
                    isinstance(i, str) and CLIP_ID_RE.match(i) for i in ids):
                raise BadRequest('ids ต้องเป็นรายการรหัสปลายทาง')
            return {'results': (start_restream if rest == ['start'] else stop_restream)(stage, ids)}
        if rest == ['dest']:
            if body.get('id') is not None and not (isinstance(body['id'], str) and CLIP_ID_RE.match(body['id'])):
                raise BadRequest('รหัสปลายทางไม่ถูกต้อง')
            return save_restream_dest(stage, body)
        if len(rest) == 3 and rest[0] == 'dest' and rest[2] == 'delete' and CLIP_ID_RE.match(rest[1]):
            delete_restream_dest(stage, rest[1])
            return {'ok': True}
        if rest == ['pin']:
            return {'ok': True}   # the page checks a typed PIN before keeping it
        raise BadRequest('ไม่รู้จักคำสั่ง restream นี้')


def check_config():
    problems = []
    if not WEB_USER or len(WEB_PASSWORD) < 12:
        problems.append('WEB_USER and WEB_PASSWORD (12+ chars) are required')
    if not MEDIAMTX_API_PASS:
        problems.append('MEDIAMTX_API_PASS is required')
    for k in ('WEB_PASSWORD', 'SRT_PASSPHRASE', 'MEDIAMTX_API_PASS'):
        if 'change-me' in os.environ.get(k, ''):
            problems.append(f'{k} still has the example value')
    if RESTREAM_PIN and not re.fullmatch(r'[A-Za-z0-9._~-]+', MEDIAMTX_API_PASS):
        problems.append('MEDIAMTX_API_PASS must be letters, digits or . _ ~ - when RESTREAM_PIN is set: '
                        'Restream puts it in an SRT streamid, where : & = and friends change its meaning')
    if RESTREAM_PIN and (len(RESTREAM_PIN) < 12 or RESTREAM_PIN.isdigit()):
        problems.append('RESTREAM_PIN must be 12+ characters and not only digits (or unset to keep restream off): '
                        'any holder of the shared web login can try values')
    partial = [k for k in S3_REQUIRED if not S3[k]]
    if 0 < len(partial) < len(S3_REQUIRED):
        problems.append('S3 is half-configured, missing: ' + ', '.join('S3_' + k for k in partial))
    if problems:
        raise SystemExit('live-clip refuses to start: ' + '; '.join(problems))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    check_config()
    for d in (DVR, CLIPS, SETTINGS, REPLAYS, RESTREAM):
        d.mkdir(parents=True, exist_ok=True)
    recover_interrupted()
    recover_replays()
    resume_restreams()
    threading.Thread(target=janitor_loop, daemon=True).start()
    threading.Thread(target=replay_watch_loop, daemon=True).start()
    threading.Thread(target=restream_watch_loop, daemon=True).start()
    port = int(os.environ.get('PORT', '8080'))
    log.info('live-clip on :%d, storage=%s', port, 'wasabi' if S3_ON else 'local only')
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()


if __name__ == '__main__':
    main()
