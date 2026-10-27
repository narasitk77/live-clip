# เพิ่ม "รับสัญญาณสด SRT/RTMP" ให้ WhistleCutter — สิ่งที่อ่านเจอ และพรอมป์สำหรับ Claude Code

อ่านโค้ด WhistleCutter (`~/WhistleCutter` @ main b93f331, 27 ต.ค. 2026) ทุกส่วน: backend, frontend, electron, ASR, เอกสาร, proxy/plugin
เทียบกับระบบ live-clip ที่ทดสอบผ่านแล้ว (github.com/narasitk77/live-clip) เพื่อหาว่าฟีเจอร์รับสัญญาณควรไปอยู่ตรงไหน

## 1. สิ่งที่พบ (ตัดสินรูปแบบงาน)

| ข้อเท็จจริง | ผลต่อการออกแบบ |
|---|---|
| WhistleCutter เป็นแอปเดสก์ท็อป: Electron spawn uvicorn ตัวเดียวจาก `backend/.venv` บน MacBook Air ของคนตัด ไม่มี Docker | ตัวรับสัญญาณต้องเป็น **โปรเซสที่ backend spawn เอง** ไม่ใช่ MediaMTX ใน Docker แบบ live-clip |
| `./ffmpeg` ที่ commit ไว้ (8.1 arm64) มี rtmp + `-listen 1`, hls muxer ครบทุก flag ที่ live-clip ใช้, **อ่าน rtsp ได้** แต่ **ไม่มี protocol srt** | รับ RTMP ได้ด้วย ffmpeg เดิม; รับ **SRT ต้องมี MediaMTX** (มี release `darwin_arm64` 27 MB, binary เดียว ไม่มี dependency) |
| มีโหมด Live อยู่แล้ว (Highlight: YouTube live ผ่าน yt-dlp + PROGRAM-DATE-TIME ใน `services/pylive_engine.py`, `routers/highlight.py`, `components/highlight/LivePicker.tsx`) | ครึ่งหลังของท่อมีครบ: เลือก segment ตาม PDT, concat demuxer, `-ss` หลัง `-i`, ต่อหลายช่วง, หน้าตรวจช็อต, เรนเดอร์แนวตั้ง, อัป Drive |
| `WHISTLECUTTER_LIVE_TC_GUIDE.md` §2.3 เขียนไว้เองว่า "เล่นจาก playlist ของเราเองด้วย hls.js = ทางที่ถูกต้องกว่า ยังไม่ได้ทำ" | งานนี้คือการทำ §2.3 โดยให้ ffmpeg เป็นคนเขียน playlist จากสัญญาณที่รับเข้ามา |
| frontend ไม่มี hls.js; CSP `script-src 'self'` ห้ามโหลดจาก CDN; `media-src/connect-src` ยอม `http://127.0.0.1:*` | hls.js ต้องเป็น npm dependency; เสิร์ฟ HLS จาก backend origin ได้โดยไม่แตะ CSP |
| backend bind 127.0.0.1 พอร์ตสุ่ม | พอร์ตรับสัญญาณต้องคงที่และเป็นตัวรับเองที่ bind 0.0.0.0 (macOS จะถามไฟร์วอลล์ครั้งแรก) |
| Typhoon ASR รันในเครื่องอยู่แล้ว (`backend/typhoon_worker.py`, silence-chunking) | คลิปที่ตัดจากสัญญาณสดได้บทถอดเสียงไทยทันทีด้วยของเดิม |

## 2. ทางที่แนะนำ: MediaMTX เป็นตัวรับ (ได้ทั้ง SRT และ RTMP) + ffmpeg เขียนบัฟเฟอร์

```
vMix/Wirecast ──SRT :8890 (passphrase) หรือ RTMP :1935──▶ mediamtx (binary ที่ root ของ repo, backend spawn)
                                                             │ rtsp://127.0.0.1:8554/<name>
                                                             ▼
                      ffmpeg -c copy -f hls (segment 2 วิ + PROGRAM-DATE-TIME) ─▶ ~/Library/Application Support/whistlecutter/live/<name>/<session>/
                                                             │
                      hls.js ใน IngestSource.tsx ◀── GET /live/hls/… ◀─┘
                      IN/OUT = epoch จาก programDateTime ─▶ POST /live/cut ─▶ ท่อ Highlight เดิม (ShotReview → Export → Drive)
```

เหตุผลที่ไม่ใช้ ffmpeg เป็นตัวรับ SRT: binary ที่ commit ไว้ไม่มี libsrt และการเปลี่ยน ffmpeg กระทบ proxy/export/ซับ/shot detect ทั้งแอป
MediaMTX เพิ่มไฟล์เดียว ให้ทั้ง SRT (เข้ารหัส + ส่งซ้ำ) และ RTMP, มี API สถานะ, และเป็นตัวเดียวกับที่ live-clip ใช้บน NAS (กติกาทุกข้อที่พิสูจน์แล้วใช้ซ้ำได้)
ถ้าต้องการแค่ LAN ในสตูดิโอ RTMP ผ่าน `ffmpeg -listen 1` ก็พอ แต่ไม่มีการเข้ารหัสและไม่ทนเน็ตแกว่ง

## 3. คำถามที่ dev หรือนัทต้องตอบก่อนเริ่ม

1. สัญญาณมาจาก LAN เดียวกับเครื่อง Air หรือจากนอกสถานที่ (ต้อง forward พอร์ตเข้าเครื่อง Air ซึ่งเปลี่ยน IP ได้) — ถ้าเป็นนอกสถานที่ ตัวรับบน NAS (live-clip) เหมาะกว่า แล้วให้ WhistleCutter ดึง HLS จาก NAS แทน (ต้องเพิ่ม host ใน CSP หรือ proxy ผ่าน backend) เป็นคนละสถาปัตยกรรม
2. รับทีละสัญญาณพอไหม (v1) หรือหลายสัญญาณพร้อมกัน
3. หลังตัดแล้วเข้าหน้าตรวจช็อต (Highlight) อย่างเดียว หรือต้องเข้า Cutter เพื่อถอดเสียงอัตโนมัติด้วย
4. บัฟเฟอร์ 60 นาที ≈ 2.7 GB ที่ 6 Mbps บนดิสก์ Air รับได้ไหม ต้องมี guard พื้นที่ว่างแบบ live-clip ไหม
5. ช่วงที่คร่อมรอยสัญญาณขาด: ปฏิเสธ (ตาม TC guide §1.4) หรือยอมตัดพร้อมรายงาน gap (แบบ live-clip)

## 4. พรอมป์สำหรับวางใน Claude Code (เปิดในโฟลเดอร์ WhistleCutter)

```text
# งาน: เพิ่มแหล่งสัญญาณ "รับสัญญาณสด (SRT/RTMP)" ให้โหมด Live ของ WhistleCutter
# = ตัวรับ MediaMTX + บัฟเฟอร์ย้อนหลังในเครื่อง + mark In/Out ขณะสด + ตัดเข้าท่อ Highlight เดิม

## 0. อ่านก่อนแตะอะไร (เปิดไฟล์จริง อย่าเดาจากชื่อ)
- CLAUDE.md ทั้งไฟล์ (branch, ก่อน commit, หนึ่งโค้ดสองหน้าบ้าน)
- WHISTLECUTTER_LIVE_TC_GUIDE.md §1.4 (segment หาย = ปฏิเสธ ห้ามเลื่อน), §1.5 (-ss หลัง -i เท่านั้น), §2.3 ("เล่นจาก playlist ของเราเองด้วย hls.js" — งานนี้คือการทำข้อนี้)
- backend/main.py: _mount_optional, /features, _QuietPolls._NOISY, lifespan, _exit_with_parent
- backend/routers/highlight.py: cut_local (request/response shape), _one_highlight, _segment_map, _cuts_dir/_prune_cuts, APP_DIR, SAFETY_PAD
- backend/services/pylive_engine.py: download_and_cut — อ่านเอากฎ (segment ต้องต่อเนื่อง, -ss หลัง -i, libx264 veryfast crf18 -r 25 aac 192k, timeout, window head/content) ห้าม import
- backend/services/video_editor.py: _find_ffmpeg, probe_duration, _proxy_jobs/_proxy_lock (แบบของ background job)
- frontend/src/components/HighlightApp.tsx (แท็บแหล่งสัญญาณ, สวิตช์ render, useFeature)
- frontend/src/components/highlight/FileSource.tsx (ต้นแบบ UI ทั้งหน้า: handleCut, keydown guard, OUTPUT_FPS)
- frontend/src/store/highlightStore.ts (filePath/filePairs, addFilePair, startScriptJob, reset)
- frontend/src/types/highlight.ts (SourceMode, FilePair, HighlightClip, Segment)
- frontend/src/hooks/useFeature.ts, frontend/src/utils/liveClock.ts (secondsTc), frontend/test/liveClock.test.mjs, backend/test_clip_qc.py (รูปแบบเทส)
- ต้นแบบที่ทำงานจริงบน NAS แล้ว (อ่านอย่างเดียว ให้ port โค้ดมา ห้าม import ข้าม repo): https://github.com/narasitk77/live-clip
  - mediamtx/mediamtx.yml + mediamtx/dvr.sh: ค่าตั้ง MediaMTX, คำสั่ง ffmpeg เขียน HLS, กติกา SIGINT ครั้งเดียว, session continuation ภายใน RECONNECT_MIN, ห้าม append ลง playlist ว่าง
  - app/app.py: parse_pdt / parse_playlist / plan_cut, pin_segments / run_cut (output-side -ss), regex แปลง PDT offset +0700→+07:00
  - app/test_app.py: เคสของ plan_cut · app/index.html: คณิต player time ↔ programDateTime + ค่า hls.js
  - README.md "ตั้งค่าตัวส่ง": ตาราง vMix/Wirecast · test/pub.sh: publisher ปลอม

## 1. กติกาที่ห้ามหลุด
1. clone นี้ checkout main อยู่ ห้ามทำบน main: `git fetch origin && git worktree add ../wc-live-ingest -b feat/live-ingest origin/dev` ทำทุกอย่างใน worktree นั้น จบงาน merge เข้า dev เท่านั้น
2. handler ทุกตัว sync def (uvicorn worker เดียว) · ทุกคำสั่ง ffmpeg/mediamtx มี stdin=DEVNULL + timeout · ไม่ import ของหนักตอนบูต
3. งานนี้ additive ล้วน ห้ามแก้: services/pylive_engine.py, components/highlight/LivePicker.tsx, App.tsx, electron/*, resolve-plugin/*, frontend/index.html (CSP), ไฟล์ ffmpeg ที่ root
4. ข้อผิดพลาดต้องดัง: ตัดไม่ได้ = HTTPException พร้อมเหตุผลไทย ห้ามคืน 200 กับ clips ว่าง · segment หาย/สัญญาณขาดกลางช่วง = ปฏิเสธช่วงนั้น ห้ามเลื่อนจุดตัดหรือตัดสั้นเงียบ ๆ · playlist ไม่มี PROGRAM-DATE-TIME = raise ห้ามเดา · mediamtx หรือ ffmpeg ตาย = state 'failed' พร้อม 3 บรรทัดท้ายของ log ไม่ใช่ "รอสัญญาณ" ตลอดกาล
5. commit style ของ repo: `feat(live): <ประโยคอังกฤษตัวเล็ก> -- <เหตุผล>` + Co-Authored-By ตาม session reminder
6. ของที่ต้องให้คนลองเอง → TEST_PENDING_TH.md ใต้ `## ระยะ 4 · รับสัญญาณสด (<วันที่> · commit a..b)` (Highlight = งานแอปอย่างเดียว = ระยะ 4) ใส่บรรทัด `เครื่องเช็คเองแล้ว: …` ก่อนรายการ

## 2. ข้อเท็จจริงที่ล็อกไว้แล้ว (เช็คจริง 27 ต.ค. 2026 ห้ามขัด)
- ./ffmpeg (8.1 arm64 ที่ commit ไว้) มี rtmp/rtmps + -listen 1, demuxer rtsp, muxer hls ครบ flags delete_segments/append_list/program_date_time/omit_endlist/temp_file/independent_segments, libx264, h264_videotoolbox, aac, lavfi testsrc/sine — แต่ **ไม่มี protocol srt** → ตัวรับ SRT คือ MediaMTX
- MediaMTX มี release darwin_arm64 (binary เดียว ~28 MB, MIT): https://github.com/bluenviron/mediamtx/releases — ดาวน์โหลด `mediamtx_v<ver>_darwin_arm64.tar.gz` วาง `mediamtx` ที่ root ของ repo ข้าง ffmpeg (git lfs หรือ commit ตรงเหมือน ffmpeg ตามที่ repo ทำอยู่) + `mediamtx.yml` ที่ root
- frontend ไม่มี hls.js; Chromium เล่น .m3u8 ไม่ได้; CSP ห้าม CDN → hls.js ต้องเป็น npm dependency; media-src/connect-src ยอม http://127.0.0.1:* → เสิร์ฟ HLS จาก backend origin ไม่แตะ CSP
- backend bind 127.0.0.1 พอร์ตสุ่ม → mediamtx เป็นตัว bind 0.0.0.0 บนพอร์ตคงที่ (SRT UDP 8890, RTMP TCP 1935); API ของ mediamtx bind 127.0.0.1:9997 เท่านั้น; RTSP 127.0.0.1:8554 เท่านั้น (ให้ ffmpeg อ่านกลับ)

## 3. mediamtx.yml (ไฟล์ใหม่ที่ root) — port จาก live-clip/mediamtx/mediamtx.yml แล้วปรับ
- srt: yes (:8890) · rtmp: yes (:1935) · rtsp: yes แต่ rtspAddress: 127.0.0.1:8554 · api: yes apiAddress: 127.0.0.1:9997 · ปิด hls/webrtc/playback/metrics/pprof/moq
- authInternalUsers: publish ได้ทุก IP (ด่านคือ srtPublishPassphrase สำหรับ SRT และ stream key ใน path สำหรับ RTMP — ตรวจชื่อ path ตาม regex `~^[a-z0-9_-]{1,32}$`); read ได้เฉพาะ 127.0.0.1; api ได้เฉพาะ 127.0.0.1
- srtPublishPassphrase มาจาก env MTX_PATHDEFAULTS_SRTPUBLISHPASSPHRASE ที่ backend ส่งตอน spawn (10–79 ตัวอักษร); RTMP key = ชื่อ path เอง (rtmp://<ip>:1935/<name>) จึงต้องให้ผู้ใช้ตั้ง name ที่เดายาก หรือใช้ SRT เมื่ออยู่นอก LAN
- ไม่ใช้ runOnAvailable (backend เป็นคนคุม ffmpeg เอง เพื่อให้ registry/สถานะอยู่ที่เดียว)

## 4. backend/services/live_ingest.py (ไฟล์ใหม่)
import ได้แค่ stdlib + services.video_editor (_find_ffmpeg, probe_duration) ห้าม import routers.highlight / pylive_engine / yt_dlp / scipy / PIL
ค่าคงที่: LIVE_DIR = ~/Library/Application Support/whistlecutter/live (override env WC_LIVE_DIR) · SEG_SECONDS=2 · RECONNECT_MIN=10 · NAME_RE ^[a-z0-9_-]{1,32}$ · SESSION_RE ^[0-9]{8}-[0-9]{6}$ · SEG_RE ^seg_[0-9]{8}[.]ts$ · class ClipError(Exception)
- _find_mediamtx(): ข้าง ffmpeg ที่ root (แบบ _find_ffmpeg) ไม่เจอ = raise ชัด ๆ ว่าต้องวาง mediamtx ที่ root
- receiver: start_receiver(passphrase) spawn `mediamtx <root>/mediamtx.yml` ครั้งเดียวต่อโปรเซส backend (env MTX_PATHDEFAULTS_SRTPUBLISHPASSPHRASE) → รอ GET http://127.0.0.1:9997/v3/paths/list ตอบ (≤10 วิ) ไม่งั้น state 'failed' พร้อม log · stop_receiver() = SIGTERM ครั้งเดียว + wait 10 วิ
- registry dict + threading.Lock แบบ _proxy_jobs: {name: {proc, thread, stop_event, session_dir, buffer_minutes, started, error}}
- start(name, buffer_minutes=60): idempotent · เลือก session dir ตาม dvr.sh (ต่อ session ล่าสุดถ้า index.m3u8 mtime ใหม่กว่า RECONNECT_MIN นาที และมีบรรทัด seg_; ห้าม append ลง playlist ว่าง; ไม่งั้นสร้าง <UTC YYYYmmdd-HHMMSS>) · ลบ session เก่ากว่า 24 ชม. · supervisor thread วน:
    ffmpeg -nostdin -hide_banner -loglevel warning -analyzeduration 3000000 -probesize 10000000
      -rtsp_transport tcp -i rtsp://127.0.0.1:8554/<name>
      -map 0:v? -map 0:a? -c copy -f hls -hls_time 2 -hls_list_size <buffer_minutes*60//2>
      -hls_flags delete_segments+append_list+program_date_time+omit_endlist+temp_file+independent_segments
      -hls_segment_filename <dir>/seg_%08d.ts <dir>/index.m3u8
  (ยังไม่มีสัญญาณ: mediamtx ตอบ 404 → ffmpeg ออกทันที → รอ 2 วิ แล้วลองใหม่ = state 'listening'; ffmpeg ออกหลังเคยรับ = publisher หลุด → รอ 1 วิ รันใหม่ ต่อ session ตามกติกา; error ติดกัน 3 ครั้งใน 10 วิ ที่ไม่ใช่ 404 → 'failed')
- stop(name): stop_event → proc.send_signal(SIGINT) ครั้งเดียว → wait 15 วิ → ค่อย kill · ห้ามส่งสัญญาณซ้ำ (ครั้งที่สองทำ index.m3u8 เหลือ 0 byte) · stop_all() สำหรับ lifespan (หยุด ffmpeg ทุกตัวแล้วค่อย mediamtx)
- status(name) → {state: off|listening|recording|failed, session, srt_url, rtmp_url, oldest, newest (epoch), segments, bytes, buffer_minutes, receiving (จาก mediamtx API paths/list ready), kbps, detail} · recording = ffmpeg alive และ index.m3u8 mtime < 10 วิ · srt_url = srt://<lan_ip>:8890?streamid=publish:<name> · rtmp_url = rtmp://<lan_ip>:1935/<name> · lan_ip จาก socket (UDP connect 8.8.8.8:80 แล้ว getsockname) fallback 127.0.0.1 · passphrase ไม่อยู่ใน status (UI อ่านจาก setting เอง)
- parse_pdt / parse_playlist / read_segments (cache ตาม mtime) / plan_cut: port จาก live-clip/app/app.py ตรง ๆ แล้วเข้มขึ้น: playlist ไม่มี PDT = ValueError; gap > 0.5 วิ ในช่วง = ClipError("สัญญาณขาดกลางช่วง {gap:.1f} วิ เลือกช่วงใหม่ให้ไม่คร่อมรอยขาด")
- cut_range(name, session, t_in, t_out, pad, out_path, fps=25): plan_cut(t_in-pad, t_out+pad) → mkdtemp(prefix="cutscript_live_") → os.link segment ที่เลือก (FileNotFoundError = ClipError "ภาพช่วงต้นคลิปเพิ่งหลุดบัฟเฟอร์"; OSError อื่น = copy2) → list.ffconcat (file + duration ทุกตัว) →
    ffmpeg -nostdin -hide_banner -loglevel error -y -f concat -safe 0 -i list.ffconcat -map 0:v? -map 0:a?
      -ss <offset> -t <dur> -r 25 -fps_mode cfr -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart <out>
  (-ss หลัง -i เท่านั้น; timeout max(600, dur*8); probe_duration ห่างจาก dur ไม่เกิน 1.0 วิ ไม่งั้น ClipError; ลบ work dir ใน finally) → คืน {path, duration, head, content}

## 5. backend/routers/live_ingest.py (ไฟล์ใหม่) — mount ด้วย _mount_optional("live_ingest", "Live ingest") ใน main.py
- import บนสุดแค่ fastapi/pydantic/services.live_ingest; `from routers.highlight import _one_highlight, _cuts_dir, _prune_cuts, safe_filename, _sec_to_clock` และ `from services import vertical_render` lazy ในตัว cut เท่านั้น
- POST /live/start {name, buffer_minutes=60} → status (start_receiver ถ้ายังไม่รัน; passphrase อ่านจาก setting ที่ UI ส่งมาครั้งแรกใน body `passphrase` และเก็บในหน่วยความจำของโปรเซส ไม่เขียนไฟล์ ไม่ log)
- GET /live/status?name= → status ('off' ไม่ใช่ 404)
- POST /live/stop {name} → status
- GET /live/hls/{name}/{session}/{file}: regex ทั้งสาม, resolve ต้องอยู่ใต้ LIVE_DIR; index.m3u8 → แปลง PDT +0700→+07:00 แล้วส่ง application/vnd.apple.mpegurl + Cache-Control no-store; seg → FileResponse video/mp2t
- POST /live/cut {name, session?, pairs:[{start: epoch วินาที, end, label=""}], pad=SAFETY_PAD} → shape เดียวกับ /highlight/cut-local ทุกคีย์ {out_dir, clips, segments:[{label,at,in_at,out_at,end}], pruned, error, log} · ชื่อไฟล์ safe_filename(i, HH.MM.SS เวลาไทยของ start, label, out_dir) · piece {path,label,duration,head,content} → _one_highlight → _prune_cuts · ช่วงที่ ClipError = log "❌ [01] <เหตุผล>" แล้วไปช่วงถัดไป · ไม่ได้สักช่วง → HTTPException 400 detail ไทย

## 6. backend/main.py — 3 จุด
1. _mount_optional("live_ingest", "Live ingest") ต่อจาก speakers พร้อมคอมเมนต์ why
2. _QuietPolls._NOISY เพิ่ม "/live/status" และ "/live/hls/"
3. lifespan หลัง yield: try: services.live_ingest.stop_all() except: logger.warning

## 7. frontend
1. `cd frontend && npm i hls.js@^1.7` commit ทั้ง package.json และ package-lock.json (wc-deps.sh จะให้ทุกเครื่องรัน setup.command หนึ่งครั้ง — จดใน TEST_PENDING)
2. types/highlight.ts: SourceMode เพิ่ม 'ingest' · IngestPair {id, start: epoch วินาที, end, label} · IngestStatus ตาม status
3. store/highlightStore.ts: ingestName ('cam1'), ingestSession, ingestPairs ใน type + initial · addIngestPair/updateIngestPair/removeIngestPair ก๊อปจาก addFilePair ฯลฯ · เติม ingestPairs: [] ใน reset ของ startScriptJob
4. components/HighlightApp.tsx: แท็บ ['ingest', 'รับสัญญาณสด'] · useFeature('live_ingest') → ไม่พร้อม = ปุ่ม disabled + title=error · render <IngestSource /> เมื่อ sourceMode === 'ingest'
5. components/highlight/IngestSource.tsx (ใหม่ ต้นแบบ = FileSource.tsx ไม่ใช่ LivePicker.tsx):
   - แผงซ้าย: ชื่อสัญญาณ (NAME_RE) · passphrase SRT (ช่อง password, สุ่มให้ปุ่มเดียว) · บัฟเฟอร์ (นาที) · ปุ่มเริ่มรับ → POST /live/start · แสดง srt_url + rtmp_url + ปุ่มคัดลอก + คำแนะนำตั้งค่า: "vMix/Wirecast: SRT Caller → <srt_url> passphrase=<…> หรือ RTMP → <rtmp_url> · H.264 + AAC สเตอริโอแทร็กเดียว · keyframe ทุก 1–2 วิ · ~6 Mbps" · poll GET /live/status ทุก 2 วิ แสดงสถานะ (รอสัญญาณ / กำลังรับ <kbps> / ล้มเหลว+detail) และช่วงบัฟเฟอร์ HH.MM.SS–HH.MM.SS · ปุ่มหยุดรับ (ยืนยัน)
   - name/passphrase จำด้วย window.electronAPI.writeSetting('liveIngest', …)/readSetting (secrets.json) ห้ามใส่ passphrase ใน URL/log/clientLog
   - player: <video muted playsInline> + new Hls({liveSyncDurationCount: 3, maxBufferLength: 30, backBufferLength: 90}) loadSource `${backendUrl}/live/hls/${name}/${session}/index.m3u8` เมื่อ state === 'recording'; hls.destroy() ใน cleanup และเมื่อ session เปลี่ยน · ปุ่ม −30/−5/+5/ไปขอบสด (clamp ใน video.seekable)
   - นาฬิกาใต้ player = thaiClock(pdtAt(frags, video.currentTime)) ไม่ใช่ currentTime · IN/OUT → addIngestPair({start: epoch, end: epoch, label}) · Space/←→ (1/25 วิ) ก๊อป handler + guard INPUT/TEXTAREA จาก FileSource · รายการช่วง แก้ label/ลบ เหมือน FileSource
   - "ตัดทั้งหมด" → POST /live/cut แล้วก๊อปบล็อกหลัง fetch ของ FileSource.handleCut ทุกบรรทัด (setClips / setSegments / setActiveClip(0) / setStep('review'))
   - ไม่มี TransportBar/waveform · ไม่แตะ delay, pylive_delay.json, OCR, frameMoment, getMediaReferenceTime
6. utils/ingestClock.ts (pure): pdtAt(frags, t) → epoch|null, mediaAt(frags, epoch) → player วินาที|null (port จาก live-clip app/index.html), thaiClock(epoch) → 'HH.MM.SS' ด้วย secondsTc((floor(epoch)+25200) % 86400)
7. test/ingestClock.test.mjs แบบ liveClock.test.mjs: t กลาง fragment, ก่อน fragment แรก (null), fragment ไม่มี PDT (null), mediaAt(pdtAt(t)) ≈ t, ข้ามเที่ยงคืน · ต้องต่อท้ายใน script "test" ของ frontend/package.json

## 8. เทส backend: backend/test_live_ingest.py (standalone แบบ test_clip_qc.py, PASS/FAIL + FAILURES, exit 1; WC_LIVE_DIR ชี้ tempdir; พอร์ต SRT/RTMP ใช้ค่าจาก env เทสเพื่อไม่ชนของจริง)
1. unit plan_cut: port เคสจาก live-clip/app/test_app.py + ช่วงมี gap ต้อง raise + playlist ไม่มี PDT ต้อง raise
2. end-to-end SRT: start_receiver(passphrase ทดสอบ) + start("t", 2) → ส่งด้วย ./ffmpeg ไม่ได้ (ไม่มี srt) → ใช้ RTMP สำหรับ e2e ในเครื่อง: `./ffmpeg -nostdin -re -f lavfi -i testsrc=size=1280x720:rate=25 -f lavfi -i sine=frequency=1000:sample_rate=48000 -c:v libx264 -preset veryfast -g 50 -keyint_min 50 -sc_threshold 0 -pix_fmt yuv420p -c:a aac -b:a 128k -t 20 -f flv rtmp://127.0.0.1:<port>/t` → รอ ≥ 4 segment (≤30 วิ) → ทุก segment มี PDT เรียงเพิ่ม dur ≈ 2±0.5 · status recording · receiving จาก API = true
3. ตัดช่วง 4.0 วิ เริ่มกลาง segment จบก่อนขอบสด 3 วิ → probe_duration ±1.0 วิ, มี Video และ Audio, 25 fps
4. ตัดช่วงเดิมหลัง stop("t") ยังได้ (hard link) · stop คืนใน 15 วิ · index.m3u8 ยังมีบรรทัด seg_
5. start แล้ว stop ทันทีตอน listening → ≤ 5 วิ ไม่ค้าง
6. SRT passphrase: ทดสอบด้วย mediamtx API ไม่ได้ส่งจาก ffmpeg เดิม → ถ้าเครื่อง dev มี ffmpeg ที่มี srt (brew) ให้ส่ง passphrase ผิดแล้ว assert ว่า paths/list ไม่มี path ขึ้น; ไม่มีก็จดใน TEST_PENDING ว่าต้องลองจาก vMix จริง
7. reconnect: publisher ส่ง 10 วิ หยุด 6 วิ ส่งใหม่ 10 วิ → session เดิม เลข seg_ ต่อเนื่อง · ช่วงคร่อมรอยขาด = ClipError · ช่วงหลังรอยขาดล้วน ๆ = ได้ปกติ
8. ปิด backend (SIGTERM uvicorn) → `pgrep -fl "mediamtx|ffmpeg.*rtsp://127.0.0.1:8554"` ต้องว่างภายใน 15 วิ

## 9. เอกสารตามธรรมเนียม repo
- TEST_PENDING_TH.md: ระยะ 4 · เครื่องเช็คเองแล้ว: test_live_ingest.py n/n · ingestClock.test.mjs · เปิดแอปจริงกับ testsrc — รายการต้องลองเอง: vMix และ Wirecast ส่ง SRT จริง (ตาราง INSTALL_TH) · macOS ถามไฟร์วอลล์ชื่อ mediamtx ครั้งแรก · mark ตอนสดแล้วเทียบเฟรม in_at กับที่เห็นตอนกด IN · deps-changed รอบแรกหลัง pull (hls.js) · ตัดตอน publisher หลุด · ปิดแอปแล้ว pgrep ต้องว่าง
- INSTALL_TH.md "## โหมด Live": เพิ่มแหล่ง "รับสัญญาณสด" + ตาราง vMix/Wirecast จาก live-clip/README.md (SRT: Caller, Stream ID publish:<name>, passphrase; RTMP: URL rtmp://<ip>:1935 key <name>; H.264 เท่านั้น ห้าม HEVC; AAC สเตอริโอแทร็กเดียว; keyframe 1–2 วิ; ~6 Mbps; Wirecast ต้อง 15.2 ขึ้นไปจึงมีช่อง Stream ID)
- HIGHLIGHT_MODE_PLAN.md: แถว "ทำเสร็จแล้ว" + ตารางไฟล์ · WHISTLECUTTER_LIVE_TC_GUIDE.md: §2.3 ทำแล้วสำหรับแหล่งรับสัญญาณสด + §1.6 "บัฟเฟอร์ในเครื่อง: PDT = นาฬิกา Mac ตอน ffmpeg เขียน segment, ไม่มี channel delay, ช่วงคร่อมรอยขาดถูกปฏิเสธ" · README.md หนึ่งบรรทัด · wc-doctor.command: เช็กว่ามี mediamtx ที่ root และ `./mediamtx --version` ตอบ

## 10. สิ่งที่ไม่ทำในรอบนี้ (non-goals)
- หลายสัญญาณพร้อมกัน, relay แยกให้ Wirecast (latency), โหมดตัดเร็ว -c copy (review step ต้องการ 25 fps), ถอดเสียงสดจากบัฟเฟอร์, รับจาก NAS live-clip, TS timestamp wrap หลัง 26 ชม., Windows/Resolve plugin
```

## 5. บั๊กที่ live-clip เจอมาแล้ว (ใส่ไว้ในพรอมป์แล้ว แต่ย้ำ)
- ส่งสัญญาณหยุดให้ ffmpeg สองครั้ง → index.m3u8 เหลือ 0 byte → บัฟเฟอร์ทั้งก้อนใช้ไม่ได้
- `-ss` ก่อน `-i` บน MPEG-TS → คลิปเริ่มช้า 1 วินาทีทั้งที่ความยาวถูก → เทสต้องเทียบภาพไม่ใช่แค่ความยาว
- analyzeduration 1 วินาทีสั้นไปกับ keyframe ทุก 2 วินาที (OBS) → ใช้ 3 วินาที
- ตัวรับไม่รอตัวบันทึกเก่าปิดก่อนเริ่มตัวใหม่ → ต้องมี lock ต่อสัญญาณ
