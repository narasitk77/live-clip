# Runbook: live-clip บน Cloud VM (RTMP) → WhistleCutter

```
encoder หน้างาน ──RTMP TCP 1935──▶ VM Hostatom (MediaMTX → dvr.sh → HLS) ──HTTPS 443──▶ WhistleCutter ในออฟฟิศ
```

- เครื่อง: Hostatom Cloud VPS SSD3 (4 vCPU / 8 GB / 80 GB) สั่งเมื่อ 2 พ.ย. 2026 รอบบิลถึง 2 ธ.ค. 2026
- ชื่อ `live.tsd.co` ในหน้า Hostatom เป็นแค่ป้ายชื่อเครื่อง `tsd.co` ไม่ใช่โดเมนของเรา (เป็นโดเมนที่วางขายบน Afternic) ห้ามใช้ทำ DNS
- **ตัดสินใจ 3 พ.ย. 2026: ไม่ขอ DNS จาก IT ใช้ IP ตรง ๆ** ทุกที่ในไฟล์นี้ที่ยังเขียน `liveclip.thestandard.co` (ข้อ 0, 1, 3, 4) ให้อ่านเป็น `203.170.129.68` ส่วนข้อ 0 กับ 3 เก็บไว้เผื่อเปลี่ยนใจ
  - เว็บ / WhistleCutter: **`https://203.170.129.68`** (ใบรับรอง Let's Encrypt ของ IP อายุ 6 วัน Caddy ต่ออายุเอง)
  - มอนิเตอร์ทีละเวที (เปิดคนละแท็บ): `https://203.170.129.68/?stream=main` และ `?stream=xstage`
  - ตัวส่ง: `rtmp://203.170.129.68:1935`
  - ชื่อ `203-170-129-68.sslip.io` ยังใช้ได้ แต่ **เน็ตออฟฟิศ (FortiGuard DNS 192.168.22.199) บล็อก `*.sslip.io` / nip.io / traefik.me** (เจอ 3 พ.ย.) จึงใช้ IP เป็นหลัก
  - เช็กวันหมดอายุใบรับรอง IP (ต้องเลื่อนไปเองทุก 2–4 วัน): `echo | openssl s_client -connect 203.170.129.68:443 2>/dev/null | openssl x509 -noout -enddate`
  - เข้า VM: `ssh -i ~/.ssh/liveclip_ed25519 root@203.170.129.68` จากแมคนัท (ติดกุญแจแล้ว ไม่ต้องใช้รหัส)
  - ล็อกอินเว็บ/WhistleCutter: user `tsd` รหัสนัทตั้งเอง เปลี่ยนด้วย `cloud/set-web-login.sh 203.170.129.68 [user]` (พิมพ์รหัสใน Terminal ไม่ผ่านแชต) แล้วทุกคนต้องต่อใหม่
- ไฟล์ที่ใช้:
  - `cloud/rtmp-setup.sh` ตั้งเครื่อง
  - `docker-compose.rtmp.yml` + `cloud/rtmp/mediamtx.yml` รหัสแยกต่อเวที
  - `test/send_rtmp.sh` ส่งภาพทดสอบ
- **บนเครื่องนี้ให้ใช้ `docker compose ...` เฉย ๆ เสมอ ห้ามใส่ `-f docker-compose.cloud.yml`** ถ้าใส่ ระบบรหัสต่อเวทีจะหายแบบเงียบ ๆ

## 0. ทำได้เลยระหว่างรอเครื่อง

1. เตรียมข้อความถึง IT ไว้ ส่งได้ทันทีที่ได้ IP

   > ขอเพิ่ม DNS record ในโซน thestandard.co (Cloudflare) 1 รายการ: Type **A**, Name **liveclip**, IPv4 **&lt;IP&gt;**,
   > Proxy status **DNS only (เมฆสีเทา)**, TTL Auto
   > ใช้รับสัญญาณไลฟ์จากหน้างาน (RTMP พอร์ต 1935) และเป็นเว็บ HTTPS ของทีมตัดต่อ ถ้าเปิดเมฆส้ม พอร์ต 1935 จะใช้ไม่ได้

2. เครื่องของ editor ต้องเป็น WhistleCutter รุ่นที่มีแท็บ "จาก Stream" (อัปเดตด้วย wc-update)
3. ถาม Tlsx 2 เรื่อง: ใช้ encoder ยี่ห้ออะไร รุ่นไหน (vMix / Wirecast / OBS) และมีเครื่องสำรองไหม ถ้ามี เครื่องสำรองต้องได้ key แยก

## ทางลัด: คำสั่งเดียวจากแมค

พอได้อีเมลจาก Hostatom (IP + รหัส root) ให้รันในโฟลเดอร์ live-clip บนแมค:

```bash
cloud/up-from-mac.sh <IP>
```

คำสั่งนี้ถามรหัส root ครั้งเดียว แล้วทำข้อ 1 และข้อ 2.1–2.2 ให้ทั้งหมด:
- ตั้งเครื่อง
- ดึงไฟล์ค่าตั้งมาไว้ที่ `senders-cloud.txt` (ไฟล์ลับ ไม่ขึ้น git)
- ส่งภาพทดสอบเข้าเวที `xstage` 15 นาที
- เปิดอุโมงค์ไว้ที่ `http://127.0.0.1:18080`

เหลือแค่ทำข้อ 2.3–2.4 (เปิดเว็บ แล้วต่อ WhistleCutter) จากนั้นค่อย `passwd` เปลี่ยนรหัส root

ถ้าคำสั่งนี้ติดตรงไหน ให้ทำตามข้อ 1–2 ด้วยมือ

## 1. เครื่องพร้อม (Hostatom ส่ง IP + รหัส root มาทางอีเมล)

```bash
ssh root@<IP>
```

บน VM ให้เปลี่ยนรหัส root ที่มากับอีเมลก่อน (`passwd`) เพราะเครื่องนี้จะเก็บ key ของทุกเวที

ถ้าเครื่องไม่มี curl (Debian บางรุ่น) ให้ติดตั้งก่อน:

```bash
command -v curl >/dev/null || { apt-get update && apt-get install -y curl; }
```

จากนั้นรันคำสั่งเดียว:

```bash
curl -fsSL https://raw.githubusercontent.com/narasitk77/live-clip/main/cloud/rtmp-setup.sh | sh -s liveclip.thestandard.co
```

สคริปต์ทำสิ่งเหล่านี้:

- ติดตั้ง git และ Docker แล้ว clone รีโปไปที่ `/opt/live-clip`
- สุ่มรหัสทั้งหมด: passphrase SRT, รหัสเว็บ และรหัส publish ของเวที `main` (Tlsx), `xstage` (เวที X) และ relay
- pin อิมเมจ `sha-17b28b5` (ตัวที่ทดสอบแล้ว)
- เปิด MediaMTX + เว็บ (ยังไม่เปิด Caddy) ตรวจว่าพอร์ต 1935 รับอยู่ และเว็บอ่าน MediaMTX ได้
- ตั้ง "เก็บหลังจบ" เป็น 8 ชม. เพราะดิสก์ 80 GB เก็บ 24 ชม. ไม่พอ
- เขียนค่าตั้งของแต่ละเวที + รหัส WhistleCutter ลง `/root/live-clip-senders.txt`

สิ่งที่จะเห็นระหว่างรัน:

- **ช่วงโหลดอิมเมจ (~220 MB) ใช้เวลาหลายนาที** เพราะเน็ตต่างประเทศของ Hostatom ช้า อย่ากด Ctrl-C
- ผลที่ถูกต้องต้องมีบรรทัด `web OK` แล้วตามด้วยบรรทัด `DNS: ...`
  - ก่อน IT ทำ DNS จะขึ้น `does not point at` ซึ่งเป็นเรื่องปกติ
- ถ้าขึ้น `FAIL` ให้ดู `cd /opt/live-clip && docker compose logs mediamtx web`
- ถ้าขึ้น `Could not get lock` ให้รอ 5–10 นาที แล้วรันคำสั่งเดิมซ้ำ

**รันซ้ำได้ รหัสเดิมไม่เปลี่ยน แต่ห้ามรันซ้ำระหว่างที่มีเวทีสด** เพราะสคริปต์จะ git pull และอาจสร้าง MediaMTX ใหม่ ทำให้ทั้งสองเวทีหลุด

ดูค่าตั้งด้วย `cat /root/live-clip-senders.txt` แล้วส่งให้แต่ละเวทีเฉพาะบล็อกของเวทีนั้น ห้ามแปะในแชตกลุ่ม

## 2. ทดสอบรอบแรก (ไม่ต้องรอ DNS)

**2.1 ส่งภาพทดสอบจากแมค** (รันในโฟลเดอร์ live-clip) ใส่ key ในเครื่องหมาย `'...'` เสมอ

```bash
test/send_rtmp.sh rtmp://<IP>:1935 'xstage?user=xstage&pass=<รหัส xstage>' 900
```

ภาพทดสอบมีตัวเลขเวลาของตัวเอง ละเอียด 1/100 วินาที และมีเสียง 1 kHz

**2.2 เปิดอุโมงค์ SSH ไปที่เว็บ** เปิดค้างไว้ในอีกหน้าต่าง และใช้บนแมคเครื่องเดียวกับที่จะเปิด WhistleCutter ระหว่างที่ยังไม่มี HTTPS เว็บจะเข้าได้ทางนี้ทางเดียว

```bash
ssh -N -L 18080:$(ssh root@<IP> "docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' live-clip-web"):8080 root@<IP>
```

คำสั่งนี้จะถามรหัส root สองครั้ง ถ้ารีสตาร์ท container แล้ว IP ภายในเปลี่ยน ให้รันใหม่

**2.3 เปิด `http://127.0.0.1:18080` ในเบราว์เซอร์** ใช้ user `editor` กับรหัสจากไฟล์ senders สัญญาณ `xstage` ต้องผ่านทุกข้อนี้:
- จุดเขียว (รับสัญญาณ) ติด
- จุดแดง (บันทึกบัฟเฟอร์) ติด
- มีรอบที่มีป้าย LIVE และตัวเลข "มีภาพ m:ss" เพิ่มขึ้นเรื่อย ๆ
- ไม่มีแถบเตือนสีเหลือง

**2.4 WhistleCutter**
1. หน้าแรก → **Live** → แท็บ **จาก Stream**
2. URL `http://127.0.0.1:18080` + user/รหัสเดียวกัน → **เชื่อมต่อ**
3. เลือกสัญญาณ `xstage` รอบที่เป็น LIVE → กด **ล่าสุด**
4. กด **◉ IN** แล้ว **◉ OUT** (ไม่มีคีย์ลัด) → **เพิ่ม** → **ตัดทั้งหมด (1)** → เข้าขั้นตรวจ

เกณฑ์ผ่าน:
- ในขั้นตรวจ ตัวเลขในภาพที่เฟรมแรกของช่วง ต้องตรงกับตัวเลขตอนที่กด IN คลาดได้ไม่เกิน 1 เฟรม (0.03 วิ)
- ได้ยินเสียง 1 kHz ตลอดคลิป
- ตัดคลิประหว่างที่ยังไลฟ์อยู่ได้

## 3. หลัง IT ทำ DNS

1. เช็กจากออฟฟิศ: `dig +short liveclip.thestandard.co` ต้องได้ IP ของ VM
2. บน VM: `cd /opt/live-clip && docker compose up -d` ขั้นนี้เพิ่ม Caddy เข้ามา
3. รัน `docker compose logs -f caddy` รอจนขึ้น `certificate obtained successfully`
4. เปิด `https://liveclip.thestandard.co` ในเบราว์เซอร์
5. ใน WhistleCutter กด **ตัดการเชื่อมต่อ** แล้วเชื่อมต่อใหม่ด้วย URL `https://liveclip.thestandard.co`
   - **พิมพ์ `https://` เอง** ถ้าไม่พิมพ์ แอปจะเติม `http://` ให้
   - เซิร์ฟเวอร์จะปฏิเสธพร้อมข้อความ "ใช้ https:// เท่านั้น" (กันไว้ไม่ให้รหัสวิ่งแบบไม่เข้ารหัส)
6. ตัวส่งเปลี่ยนจาก IP เป็นชื่อ `rtmp://liveclip.thestandard.co:1935`

ถ้า Caddy ขอ cert ไม่สำเร็จ ให้สั่ง `docker compose stop caddy` ทันที แล้วหาสาเหตุก่อน เพราะ Let's Encrypt ยอมให้ล้มได้แค่ 5 ครั้งต่อชั่วโมง

**ไม่อยากรอ IT:** ใช้ชื่อ `<IP ขีดแทนจุด>.sslip.io` (เช่น `203-170-129-68.sslip.io`) ซึ่งชี้กลับมาที่ IP เองทันที
บน VM: `cd /opt/live-clip && git pull -q && sh cloud/rtmp-setup.sh 203-170-129-68.sslip.io && docker compose up -d`
(ใช้สำเนาในเครื่องหลัง `git pull` ไม่ใช่ `curl` จาก main เพราะ GitHub cache ไฟล์ 5 นาที)
ใช้ได้จริงเมื่อ 3 พ.ย. 2026: ได้ cert Let's Encrypt และ DNS ออฟฟิศเปิดได้ พอ IT ทำ DNS แล้วรันคำสั่งเดิมด้วยชื่อ `liveclip.thestandard.co` แล้วแจ้ง URL ใหม่ให้ทุกคน

## 4. ทดสอบของจริง

ค่าตั้งของทุกเวทีให้เอาจากไฟล์ `/root/live-clip-senders.txt` เท่านั้น ห้ามใช้กล่อง "วิธีส่ง" ในหน้าเว็บ live-clip เพราะกล่องนั้นยังสอนรูปแบบ SRT แบบเก่าที่ใช้ไม่ได้กับเครื่องนี้

**เวที X: OBS + เราเตอร์ AIS 5G**
- Settings > Stream: Service `Custom...`
  - Server: `rtmp://liveclip.thestandard.co:1935`
  - Stream Key: `xstage?user=xstage&pass=<รหัส>`
  - ไม่ติ๊ก Use authentication
- Settings > Output (Advanced) > Streaming
  - Encoder H.264 (x264 หรือ Apple VT H264), Rate Control CBR 6000 Kbps
  - Keyframe Interval 2 s, เสียง AAC
- Settings > Advanced > Network: ติ๊ก **Dynamically change bitrate to manage congestion**
  - ฟีเจอร์นี้มีเฉพาะ RTMP ตอนเน็ต 5G ตก OBS จะลดบิตเรตเอง แทนที่จะทำภาพแตก
- ถ้ามีช่อง Multitrack Video ให้ปิด
- ทดสอบส่งยาว 1–2 ชม. จากเราเตอร์ตัวจริง แล้วดูในหน้าเว็บว่าไม่หลุด

**เวทีหลัก: Tlsx** ส่งบล็อก `main` จากไฟล์ senders

| encoder | ช่องที่ต้องกรอก |
|---|---|
| vMix | Streaming → Destination `Custom RTMP Server` → URL `rtmp://liveclip.thestandard.co:1935`, Stream Key `main?user=main&pass=<รหัส>` (ช่อง Username/Password ใน Advanced ให้เว้นว่าง) |
| Wirecast | Output Settings → RTMP Server → Address `rtmp://liveclip.thestandard.co:1935`, Stream `main?user=main&pass=<รหัส>` (ไม่ต้องกด Set Credentials) ช่อง keyframe นับเป็นเฟรม ที่ 30 fps ใส่ 60 ส่วน SRT ของ Wirecast ตั้ง latency ไม่ได้ ให้ใช้ RTMP เท่านั้น |
| OBS | เหมือนเวที X แต่ใช้ key ของ `main` |

กติกาที่ต้องบอก Tlsx:
- วิดีโอ H.264 เท่านั้น ห้าม HEVC/AV1 เสียง AAC สเตอริโอ
- ห้ามเติม `/live` ท้าย Server
- 1 key ต่อ encoder ที่ออนแอร์ 1 เครื่อง
- ทดสอบจากเน็ตหน้างานจริงก่อนวันงานอย่างน้อย 1 วัน

**ทางสำรอง** ถ้าเน็ตหน้างานบล็อกพอร์ต 1935 ให้ใช้ส่วน `SRT fallback` ในไฟล์ senders แทน (ต้องเปิด UDP 8890 ได้) มีทั้งแบบ URL สำหรับ OBS และแบบแยกช่องสำหรับ vMix

## 5. ระหว่างงาน

- **ห้ามทำสิ่งเหล่านี้ระหว่างที่มีเวทีสด** ทุกข้อทำให้ทั้งสองเวทีหลุดได้:
  - แก้ `cloud/rtmp/mediamtx.yml`
  - รัน `rtmp-setup.sh` ซ้ำ
  - `git pull`, `docker compose up -d` หรือรีสตาร์ท
  - push เข้า main ของรีโป
- ตัวส่งหลุดแล้วต่อใหม่ บัฟเฟอร์จะขาด 2–5 วิ ถ้าหลุดไม่เกิน 10 นาทีจะต่อรอบเดิม
- บั๊กที่รู้อยู่แล้ว: ใน 2 วิแรกหลังเริ่มหรือต่อใหม่ เสียงจะนำภาพ อย่ามาร์กจุดเริ่มคลิปในช่วงนั้น
- ถ้าเปิดหน้าเว็บกับ WhistleCutter พร้อมกัน ตัวเลข Mbps อาจกะพริบหายเป็นช่วง ๆ เป็นแค่การแสดงผล ไม่ได้แปลว่าสัญญาณหลุด ให้ดูจุดเขียว/แดงแทน
- ดูดิสก์ว่างได้ที่หัวหน้าเว็บ

## 6. อาการ → สาเหตุ

| อาการ | สาเหตุ / ทางแก้ |
|---|---|
| encoder ขึ้น `authentication failed` | key หรือรหัสผิด หรือเอา key ของอีกเวทีมาใช้ |
| encoder ขึ้น connection refused / timeout | ตรวจ `docker compose ps` บน VM ถ้า VM ปกติ แปลว่าเน็ตหน้างานบล็อก 1935 ให้ใช้ SRT สำรอง |
| log ขึ้น `path ... is not configured` | ใส่ `/live` ไว้ใน Server หรือพิมพ์ชื่อเป็นตัวใหญ่ |
| ภาพสลับไปมา และบัฟเฟอร์เป็นรูถี่ ๆ | encoder สองเครื่องใช้ key เดียวกัน |
| หน้าเว็บขึ้น "รับสัญญาณอยู่แต่ไม่ได้บันทึก" หรือคลิปไม่มีภาพ | encoder ส่ง HEVC/AV1 ให้เปลี่ยนเป็น H.264 |
| WhistleCutter ขึ้น "ใช้ https:// เท่านั้น" | URL ไม่มี `https://` ให้กดตัดการเชื่อมต่อ แล้วพิมพ์ใหม่พร้อม `https://` |
| WhistleCutter ต่อไม่ได้ (ก่อน DNS) | อุโมงค์ SSH ปิดอยู่ หรือรหัสผิด |
| Caddy ขอ cert ไม่ได้ | DNS ยังไม่ชี้มาที่ VM หรือเปิดเมฆส้มไว้ ให้ `docker compose stop caddy` ก่อน |

## 7. หลังจบงาน (ทำตามลำดับ)

1. ดาวน์โหลดคลิปที่ต้องเก็บให้ครบ
2. บน VM: `cd /opt/live-clip && docker compose down -v` ขั้นนี้ลบบัฟเฟอร์และคลิปทั้งหมด ย้อนกลับไม่ได้
3. ใน WhistleCutter ของทุกเครื่อง กด **ตัดการเชื่อมต่อ** เพื่อลบที่อยู่และรหัสที่จำไว้
4. แจ้ง IT ลบ DNS record `liveclip`
5. รอจน `dig +short liveclip.thestandard.co` ไม่คืนค่าอะไร แล้วค่อยยกเลิก Hostatom (ก่อน 2 ธ.ค. 2026)
   - ถ้ายกเลิกก่อน DNS หาย IP นี้อาจถูกส่งต่อให้คนอื่น และชื่อเราจะชี้ไปที่เครื่องของเขา
