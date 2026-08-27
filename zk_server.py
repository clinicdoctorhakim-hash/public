# -*- coding: utf-8 -*-
"""
zk_server.py v4 - เซิร์ฟเวอร์ทะเบียนคนไข้ + ลายนิ้วมือ
คลินิกปัตตานีการแพทย์

รันบน PC แล้วปล่อยทิ้งไว้ มือถือในวง WiFi เดียวกันเรียกใช้ได้

** PC ไม่ต้องมีเครื่องสแกน ** เพราะมือถือเป็นคนเทียบลายนิ้วมือเอง

ต้องมี:
  pip install dbfread

วิธีใช้:
  1) สร้างไฟล์ .env ไว้ข้างๆ ไฟล์นี้ (ดูตัวอย่างใน .env.example)
  2) python zk_server.py
"""

import os
import re
import sys
import json
import time
import base64
import shutil
import socket
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if os.name == "nt":
    os.system("chcp 65001 >nul")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


# ============================================================ ตั้งค่าจาก .env
def load_env():
    """อ่านค่าจากไฟล์ .env (แยกความลับออกจากโค้ด)"""
    cfg = {}
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")   # ตัดเว้นวรรคทั้งชื่อและค่า
    return cfg


ENV = load_env()
DBF_PATH   = ENV.get("DBF_PATH", r"D:\Zipdrive\foxpro\pat4.DBF")
PORT       = int(ENV.get("PORT", "8080") or 8080)
PIN        = ENV.get("PIN", "")
SECRET     = ENV.get("SECRET", "")
RELOAD_SEC = int(ENV.get("RELOAD_SEC", "60") or 60)
def _clean_host(v):
    """ตัดอักขระแปลกที่ติดมาตอนก๊อป (เช่น รูปแบบลิงก์ Markdown จาก Gmail)"""
    v = (v or "").strip()
    m = re.match(r"\s*\[([^\]]+)\]", v)      # [ที่อยู่](ลิงก์) -> เอาแค่ในวงเล็บเหลี่ยม
    if m:
        v = m.group(1)
    v = v.replace("https://", "").replace("http://", "").strip().strip("/")
    return "".join(c for c in v if c.isalnum() or c in ".-:")


WORKER     = _clean_host(ENV.get("WORKER", "hn-ocr.kabdulha2018.workers.dev"))
ADMIN_PIN  = ENV.get("ADMIN_PIN", "")     # PIN สำรอง ใช้ได้ตอนเน็ตล่ม (สิทธิ์ admin)
CF_SECRET  = ENV.get("CF_SECRET", "")     # UPLOAD_SECRET ของ Cloudflare (PC <-> worker เท่านั้น)
FINGERS    = ["R1", "R2"]                 # นิ้วที่เก็บ: หัวแม่มือขวา, ชี้ขวา

# ไฟล์ทั้งหมดอยู่ในโฟลเดอร์เดียวกับโปรแกรมนี้ (แนะนำวางที่ ...\\foxpro\\zk_android\\)
TPL_PATH  = os.path.join(HERE, "zk_fingers.json")   # template ลายนิ้วมือ
IMG_DIR   = os.path.join(HERE, "fp_images")         # ภาพลายนิ้วมือ
LOG_PATH  = os.path.join(HERE, "zk_access.log")     # บันทึกการเข้าใช้
QUEUE_PATH = os.path.join(HERE, "zk_queue.json")    # คิวรอเขียนลง dbf
SLOT_PATH  = os.path.join(HERE, "zk_slots.json")    # ช่องสแกนที่มีปัญหา (ห้ามแจกซ้ำ)
SYNC_PATH  = os.path.join(HERE, "zk_sync.json")     # ลายเซ็นแถวที่ซิงค์ขึ้น D1 แล้ว

MAX_PIN_FAIL = 5          # PIN ผิดกี่ครั้งถึงบล็อก
BLOCK_SEC    = 15 * 60    # บล็อกนานเท่าไหร่

PAT = []            # [{rid, number, name, surname, age, dt, address, sex, id_card}]
DBF_MTIME = 0
FP = {}             # { HN_UID: {uid, name, number, f:{R1:{t,ts,img,sid}, R2:{...}}} }
FAIL = {}           # {ip: [จำนวนครั้ง, เวลาที่บล็อกหมด]}
QUEUE = []          # [{id, kind, rid, data, who, ts, fp}]
WHO_CACHE = {}      # {pin: (เวลาที่ตรวจ, {"name":..,"role":..})} จำ 5 นาที
SID_MAX = int(ENV.get("SID_MAX", "100000") or 100000)
                    # ขีดจำกัดจาก getFPLimitCount() - วัดจากเครื่องจริงแล้ว = 100,000
                    # (= 50,000 คน ถ้าเก็บคนละ 2 นิ้ว)
SID_USED = {}       # {scanner_id: (HN_UID, นิ้ว)} - 1 ช่อง = 1 ลายนิ้วมือ
SID_HOLD = {}       # {scanner_id: (HN_UID, นิ้ว, เวลาหมดอายุ)} จองชั่วคราว
SID_LOCK = threading.Lock()
SID_BAD = {}        # {scanner_id: {state, why, found, dev}} ช่องที่มีปัญหา - ไม่แจกซ้ำ
LOCK = threading.Lock()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a)


def audit(ip, dev, action, detail=""):
    """บันทึกว่าใครทำอะไรเมื่อไหร่ (ไม่บันทึกรหัสผ่านเด็ดขาด)"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{ip}\t{dev}\t{action}\t{detail}\n")
    except Exception:
        pass


def esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ============================================================ ทะเบียนคนไข้
def load_dbf(force=False):
    """อ่าน pat4.dbf เข้าหน่วยความจำ (โหลดใหม่เมื่อไฟล์เปลี่ยน)"""
    global PAT, DBF_MTIME
    try:
        m = os.path.getmtime(DBF_PATH)
    except Exception as e:
        log("[X] เปิดไฟล์ทะเบียนไม่ได้:", DBF_PATH, e)
        return
    if not force and m == DBF_MTIME:
        return
    try:
        from dbfread import DBF
    except Exception:
        log("[X] ยังไม่ได้ลง dbfread - พิมพ์:  pip install dbfread")
        return
    t0 = time.time()
    rows = []
    try:
        tb = DBF(DBF_PATH, encoding="cp874", load=False, char_decode_errors="replace")
        for i, r in enumerate(tb, 1):
            rows.append({
                "rid": i,
                "number": r.get("NUMBER"),
                "name": (r.get("NAME") or "").strip(),
                "surname": (r.get("SURNAME") or "").strip(),
                "age": r.get("AGE"),
                "dt": str(r.get("DATE") or ""),
                "address": (r.get("ADDRESS") or "").strip(),
                "sex": (r.get("SEX") or "").strip(),
                "id_card": (r.get("ID_CARD") or "").strip(),
                "disease": (r.get("DISEASE") or "").strip(),
                "hn_uid": (r.get("HN_UID") or "").strip(),   # มีเมื่อเพิ่มคอลัมน์แล้ว
            })
    except Exception as e:
        log("[X] อ่านไฟล์ทะเบียนไม่สำเร็จ:", e)
        return
    with LOCK:
        PAT = rows
        DBF_MTIME = m
    log(f"[OK] โหลดทะเบียนคนไข้ {len(rows):,} คน ({time.time()-t0:.1f} วินาที)")


def watcher():
    while True:
        time.sleep(15 if not PAT else RELOAD_SEC)   # ยังอ่านไม่ได้ -> ลองถี่ขึ้น
        try:
            n0 = len(PAT)
            load_dbf(force=not PAT)
            if not n0 and PAT:
                log(f"[OK] อ่านทะเบียนได้แล้ว {len(PAT):,} คน")
        except Exception:
            pass


def find_patient(rid):
    for p in PAT:
        if p["rid"] == rid:
            return p
    return None


# ============================================================ UID
_uid_lock = threading.Lock()
_uid_last = [0, 0]
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _b36(n, w):
    out = ""
    while n > 0:
        out = _B36[n % 36] + out
        n //= 36
    return out.rjust(w, "0")[-w:]


def new_uid(number):
    """
    สร้าง HN_UID: "<เลขคนไข้>_P<10 ตัวฐาน36>"
    ตัว P = สร้างจากระบบนี้ (FoxPro ใช้ F) กันชนกันสนิท
    ทดสอบแล้ว: สร้างรัว 200,000 ครั้งไม่ซ้ำเลย
    """
    with _uid_lock:
        t = int(time.time() * 1000)
        if t == _uid_last[0]:
            _uid_last[1] += 1
            if _uid_last[1] > 1295:
                while int(time.time() * 1000) == t:
                    time.sleep(0.0005)
                t = int(time.time() * 1000)
                _uid_last[1] = 0
        else:
            _uid_last[0] = t
            _uid_last[1] = 0
        code = _b36(t, 8) + _b36(_uid_last[1], 2)
    hn = str(number if number is not None else "").strip() or "00000"
    return f"{hn}_P{code}"


def uid_of(rid):
    """หา HN_UID ของคนไข้จากลำดับแถว (ต้องมีคอลัมน์ HN_UID ใน dbf แล้ว)"""
    p = find_patient(rid)
    if p and str(p.get("hn_uid") or "").strip():
        return str(p["hn_uid"]).strip()
    return ""


def find_by_uid(uid):
    """หาคนไข้จาก HN_UID (ทน PACK - ลำดับแถวเลื่อนก็ยังหาถูก)"""
    uid = (uid or "").strip()
    if not uid:
        return None
    for p in PAT:
        if p.get("hn_uid") == uid:
            return p
    return None


# ============================================================ ลายนิ้วมือ
def load_fingers():
    """
    โครงสร้าง: { HN_UID: {sid, name, number, f:{R1:{t,ts,img}, R2:{...}}} }
    กุญแจเป็น HN_UID จึงทนต่อการ PACK (ลำดับแถวเลื่อนไม่กระทบ)
    """
    global FP, SID_USED
    FP = {}
    SID_USED = {}
    if not os.path.exists(TPL_PATH):
        log("ยังไม่มีไฟล์ลายนิ้วมือ (จะสร้างเมื่อเก็บครั้งแรก)")
        return
    try:
        with open(TPL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log("[X] อ่านไฟล์ลายนิ้วมือไม่ได้:", e)
        return

    old_style = 0
    for k, v in data.items():
        if not isinstance(v, dict):
            old_style += 1
            continue
        if "f" not in v:
            old_style += 1
            continue
        uid = str(v.get("uid") or k).strip()
        if not uid or uid.isdigit():        # รูปแบบเก่า (กุญแจเป็นลำดับแถว)
            old_style += 1
            continue
        v["uid"] = uid
        FP[uid] = v
        for fg, d in (v.get("f") or {}).items():
            try:
                sid = int(d.get("sid") or 0)
                if sid > 0:
                    SID_USED[sid] = (uid, fg)
            except Exception:
                pass

    n = sum(len(x.get("f") or {}) for x in FP.values())
    log(f"[OK] โหลดลายนิ้วมือ {len(FP):,} คน / {n:,} นิ้ว")
    if old_style:
        log(f"[!] ข้ามข้อมูลรูปแบบเก่า {old_style} รายการ - ต้องเก็บลายนิ้วมือใหม่")


def fp_count():
    return sum(len(v.get("f") or {}) for v in FP.values())


def load_slots():
    """โหลดรายการช่องที่มีปัญหา (จำข้ามการรีสตาร์ท)"""
    SID_BAD.clear()          # ล้างในที่เดิม ไม่สร้าง dict ใหม่
    if not os.path.exists(SLOT_PATH):
        return
    try:
        with open(SLOT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k, v in d.items():
            SID_BAD[int(k)] = v
        if SID_BAD:
            log(f"[!] มีช่องสแกนที่มีปัญหา {len(SID_BAD)} ช่อง (ระบบจะไม่แจกซ้ำ)")
    except Exception as e:
        log("[X] อ่านไฟล์ช่องสแกนไม่ได้:", e)


def save_slots():
    tmp = SLOT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in SID_BAD.items()}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SLOT_PATH)


def mark_bad(sid, why, dev=""):
    """จดว่าช่องนี้มีปัญหา - จะไม่แจกซ้ำจนกว่าจะเคลียร์"""
    SID_BAD[int(sid)] = {
        "state": "ไม่รู้จัก",
        "why": why,
        "found": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dev": dev,
    }
    try:
        save_slots()
    except Exception as e:
        log("[X] บันทึกช่องมีปัญหาไม่ได้:", e)


def next_sid():
    """
    หาช่องสแกนว่างถัดไป (1..SID_MAX) - 1 ช่อง = 1 ลายนิ้วมือ (ไม่ใช่ 1 คน)
    ตรงกับขีดจำกัดของเครื่องสแกนพอดี · ใช้ช่องที่ถูกลบไปแล้วซ้ำได้
    ต้องเรียกใต้ SID_LOCK
    """
    taken = set(SID_USED.keys()) | set(SID_BAD.keys())   # ข้ามช่องที่มีปัญหาด้วย
    now = time.time()
    for sid, h in list(SID_HOLD.items()):
        if h[2] > now:
            taken.add(sid)
        else:
            SID_HOLD.pop(sid, None)      # จองหมดอายุ - คืนช่อง
    for i in range(1, SID_MAX + 1):
        if i not in taken:
            return i
    return 0


def sid_of(uid, finger):
    """ช่องสแกนของ (คน, นิ้ว) นี้ - ถ้ายังไม่มีคืน 0"""
    rec = FP.get(uid) or {}
    d = (rec.get("f") or {}).get(finger) or {}
    try:
        return int(d.get("sid") or 0)
    except Exception:
        return 0


def hold_sid(uid, finger):
    """
    ขอช่องสแกนให้ (คน, นิ้ว) - ถ้าเคยมีแล้วคืนเลขเดิม
    จองไว้ 10 นาที ถ้าไม่เก็บจริงจะคืนช่องเอง
    """
    with SID_LOCK:
        got = sid_of(uid, finger)
        if got > 0:
            return got, "เดิม"
        now = time.time()
        for sid, h in SID_HOLD.items():
            if h[0] == uid and h[1] == finger and h[2] > now:
                return sid, "จองอยู่"
        sid = next_sid()
        if sid <= 0:
            return 0, "เต็ม"
        SID_HOLD[sid] = (uid, finger, now + 600)
        return sid, "ใหม่"


# ============================================================ ลายนิ้วมือ
def backup_fingers(keep=30):
    """สำรอง zk_fingers.json ก่อนเขียนทับ - เก็บ 30 ชุดล่าสุด"""
    if not os.path.exists(TPL_PATH):
        return
    try:
        d = os.path.join(HERE, "fp_backup")
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(TPL_PATH, os.path.join(d, f"zk_fingers_{stamp}.json"))
        fs = sorted(x for x in os.listdir(d) if x.startswith("zk_fingers_"))
        while len(fs) > keep:
            try:
                os.remove(os.path.join(d, fs.pop(0)))
            except Exception:
                pass
    except Exception as e:
        log("[!] สำรองไฟล์ลายนิ้วมือไม่สำเร็จ:", e)


def save_fingers():
    """เขียนแบบปลอดภัย - ไฟล์เดิมไม่พังถ้าไฟดับกลางคัน"""
    backup_fingers()                  # สำรองก่อนเขียนทับเสมอ
    tmp = TPL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in FP.items()}, f, ensure_ascii=False)
    os.replace(tmp, TPL_PATH)


def img_count_size():
    n = 0
    sz = 0
    try:
        for fn in os.listdir(IMG_DIR):
            p = os.path.join(IMG_DIR, fn)
            if os.path.isfile(p):
                n += 1
                sz += os.path.getsize(p)
    except Exception:
        pass
    return n, sz


# ============================================================ คิวรอเขียน dbf
def load_queue():
    global QUEUE
    if not os.path.exists(QUEUE_PATH):
        QUEUE = []
        return
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            QUEUE = json.load(f)
        if QUEUE:
            log(f"[!] มีคิวรอเขียนลง dbf {len(QUEUE)} รายการ")
    except Exception as e:
        log("[X] อ่านคิวไม่ได้:", e)
        QUEUE = []


def save_queue():
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(QUEUE, f, ensure_ascii=False, indent=1)
    os.replace(tmp, QUEUE_PATH)


def queue_add(kind, data, who, rid=None, fp=None):
    item = {
        "id": int(time.time() * 1000) % 1000000000,
        "kind": kind,
        "rid": rid,
        "data": data,
        "who": who,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fp": fp,          # template ที่รอผูกกับแถวใหม่ (เฉพาะ add)
    }
    QUEUE.append(item)
    save_queue()
    return item


# ============================================================ ความปลอดภัย
def blocked(ip):
    e = FAIL.get(ip)
    if not e:
        return False
    if e[1] and time.time() < e[1]:
        return True
    if e[1] and time.time() >= e[1]:
        FAIL.pop(ip, None)
    return False


def note_fail(ip):
    e = FAIL.get(ip) or [0, 0]
    e[0] += 1
    if e[0] >= MAX_PIN_FAIL:
        e[1] = time.time() + BLOCK_SEC
        log(f"[!] บล็อก {ip} เพราะใส่รหัสผิด {e[0]} ครั้ง (15 นาที)")
    FAIL[ip] = e


def note_ok(ip):
    FAIL.pop(ip, None)


def ask_worker(pin):
    """
    ถาม worker ว่า PIN นี้เป็นใคร (จำผลไว้ 5 นาที)
    คืน: dict = ผ่าน / None = PIN หรือ CF_SECRET ผิด / "OFFLINE" = ต่อไม่ได้ / "BLOCKED" = Cloudflare บล็อก
    """
    now = time.time()
    c = WHO_CACHE.get(pin)
    if c and now - c[0] < 300:
        return c[1]

    import urllib.request
    import urllib.parse
    import urllib.error
    url = (f"https://{WORKER}/whoami?secret={urllib.parse.quote(CF_SECRET)}"
           f"&pin={urllib.parse.quote(pin)}")
    # Cloudflare บล็อกคำขอที่ไม่มี User-Agent แบบเบราว์เซอร์ (error 1010)
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "th,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        if "error code: 1010" in body or "1010" == body.strip():
            log("[X] Cloudflare บล็อก (error 1010) - ปิด Browser Integrity Check ที่ Cloudflare")
            return "BLOCKED"
        log(f"[!] worker ปฏิเสธ (HTTP {e.code}) - ตรวจ CF_SECRET / PIN")
        return None
    except Exception as e:
        log("[!] ถาม worker ไม่ได้:", str(e)[:90])
        return "OFFLINE"

    if "error code: 1010" in body:
        log("[X] Cloudflare บล็อก (error 1010)")
        return "BLOCKED"
    try:
        j = json.loads(body)
    except Exception:
        log("[!] worker ตอบไม่ใช่ JSON:", body[:80])
        return "OFFLINE"
    if j.get("ok"):
        u = {"name": j.get("name", "?"), "role": j.get("role", "worker")}
        WHO_CACHE[pin] = (now, u)
        return u
    log(f"[!] worker ปฏิเสธ: {j.get('err', '')}")
    return None


def check_auth(b, ip):
    """
    ตรวจ 2 ชั้น: SECRET (กันเครื่องแปลกปลอม) แล้ว PIN (ระบุตัวคนผ่าน worker)
    คืน (ผู้ใช้, ข้อความผิดพลาด)
    """
    if blocked(ip):
        return None, "ถูกบล็อกชั่วคราว - ลองใหม่ภายหลัง"

    got = str(b.get("secret", ""))
    if SECRET and got != SECRET:
        note_fail(ip)
        log(f"[!] {ip}: SECRET ไม่ตรง (ได้ยาว {len(got)} ตัว, ต้องการ {len(SECRET)} ตัว)")
        return None, "รหัสเครื่อง (SECRET) ไม่ตรงกับที่ PC ตั้งไว้"

    pin = str(b.get("pin", "")).strip()
    if not pin:
        note_fail(ip)
        return None, "ยังไม่ได้ใส่ PIN"

    u = ask_worker(pin)

    if u == "BLOCKED":
        if ADMIN_PIN and pin == ADMIN_PIN:
            note_ok(ip)
            log("[!] Cloudflare บล็อก - ใช้ PIN สำรองของ admin แทน")
            return {"name": "admin(สำรอง)", "role": "admin"}, None
        return None, ("Cloudflare บล็อกการตรวจรหัส (error 1010)\n"
                      "ปิด Browser Integrity Check ที่ Cloudflare\n"
                      "หรือใช้ PIN สำรองของ admin")

    if u == "OFFLINE":
        if ADMIN_PIN and pin == ADMIN_PIN:
            note_ok(ip)
            return {"name": "admin(สำรอง)", "role": "admin"}, None
        return None, "PC ต่อ worker ไม่ได้ (เน็ตล่ม) - ใช้ PIN สำรองของ admin"

    if not u:
        note_fail(ip)
        return None, "PIN ไม่ถูกต้อง หรือ CF_SECRET ที่ PC ไม่ตรงกับ Cloudflare"

    note_ok(ip)
    return u, None


def need_role(u, role):
    """admin ทำได้ทุกอย่าง / trusted ทำได้ระดับ trusted ลงมา"""
    order = {"worker": 1, "trusted": 2, "admin": 3}
    return order.get(u.get("role", "worker"), 1) >= order.get(role, 1)


def slot_map():
    """แผนที่ช่องสแกน - ใช้ร่วมกันทั้งหน้าเว็บและแอป"""
    rows = {}
    for sid, v in SID_USED.items():
        u, fg = (v if isinstance(v, tuple) else (v, ""))
        p2 = find_by_uid(u)
        rows[str(sid)] = {
            "uid": u, "finger": fg,
            "name": (p2["name"] + " " + p2["surname"]) if p2 else "(ไม่พบในทะเบียน)",
            "number": p2["number"] if p2 else "",
        }
    bad = {str(k): v for k, v in SID_BAD.items()}
    return {"ok": True, "used": rows, "bad": bad,
            "n_used": len(rows), "n_bad": len(bad),
            "free": SID_MAX - len(rows) - len(bad), "max": SID_MAX,
            "people": len(FP), "warn90": (len(rows) >= SID_MAX * 0.9)}


# ============================================================ ซิงค์ขึ้น D1
SYNC_COUNT = {"n": 0, "nouid": 0}     # จำผลนับล่าสุด ไม่ต้องนับใหม่ตอนกำลังซิงค์
SYNC_STATE = {"running": False, "done": 0, "total": 0, "saved": 0,
              "msg": "", "err": "", "at": 0}


def row_sig(p):
    """ลายเซ็นสั้นของแถว - เปลี่ยนเมื่อข้อมูลเปลี่ยน ใช้หาว่าแถวไหนต้องส่งใหม่"""
    raw = "|".join(str(p.get(k) or "") for k in
                   ("number", "name", "surname", "age", "address",
                    "sex", "id_card", "disease", "date"))
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()[:10]


def load_sync():
    if not os.path.exists(SYNC_PATH):
        return {}
    try:
        with open(SYNC_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log("[!] อ่านไฟล์ซิงค์ไม่ได้:", e)
        return {}


def save_sync(d):
    tmp = SYNC_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, SYNC_PATH)


def worker_post(path, body):
    """ส่งข้อมูลไป worker (ใส่ User-Agent กัน Cloudflare บล็อก)"""
    import urllib.request
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://{WORKER}{path}", data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0) ZKClinic"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def do_sync(pin, full=False):
    """ส่งเฉพาะแถวที่เปลี่ยน (หรือทั้งหมดถ้า full=True) ขึ้น D1"""
    global SYNC_STATE
    if SYNC_STATE["running"]:
        return
    SYNC_STATE = {"running": True, "done": 0, "total": 0, "saved": 0,
                  "msg": "กำลังเตรียม...", "err": "", "at": time.time()}
    try:
        if not WORKER or not CF_SECRET:
            raise RuntimeError("ยังไม่ได้ตั้ง WORKER/CF_SECRET ใน .env")
        old = {} if full else load_sync()
        todo = []
        nouid = 0
        for p in PAT:
            uid = (p.get("hn_uid") or "").strip()
            if not uid:
                nouid += 1
                continue
            sig = row_sig(p)
            if old.get(uid) != sig:
                todo.append((uid, sig, p))
        SYNC_STATE["total"] = len(todo)
        if nouid:
            log(f"[!] ข้าม {nouid:,} แถวที่ยังไม่มี HN_UID (รัน add_hnuid.prg)")
        if not todo:
            SYNC_STATE["msg"] = "ตรงกันอยู่แล้ว - ไม่มีอะไรต้องส่ง"
            return

        log(f"[Sync] เริ่มส่ง {len(todo):,} แถวขึ้น D1")
        CH = 400
        sent = {}
        for i in range(0, len(todo), CH):
            part = todo[i:i + CH]
            rows = []
            for uid, sig, p in part:
                rows.append({
                    "hn_uid": uid, "number": p.get("number") or 0,
                    "name": p.get("name") or "", "surname": p.get("surname") or "",
                    "age": p.get("age") or 0, "dt": str(p.get("date") or ""),
                    "address": p.get("address") or "", "sex": p.get("sex") or "",
                    "dt2": str(p.get("date2") or ""), "chk": 0, "detail": "",
                    "disease": p.get("disease") or "", "id_card": p.get("id_card") or "",
                    "finger": "",
                })
            r = worker_post("/appsync", {"secret": CF_SECRET, "pin": pin, "rows": rows})
            if not r.get("ok"):
                raise RuntimeError(r.get("err", "worker ปฏิเสธ"))
            SYNC_STATE["saved"] += int(r.get("saved") or 0)
            SYNC_STATE["done"] += len(part)
            for uid, sig, p in part:
                sent[uid] = sig
            SYNC_STATE["msg"] = f"ส่งแล้ว {SYNC_STATE['done']:,}/{len(todo):,} แถว"
            if (i // CH) % 10 == 0:
                merged = dict(old)
                merged.update(sent)
                save_sync(merged)
        old.update(sent)
        save_sync(old)
        SYNC_STATE["msg"] = f"เสร็จ - ส่ง {SYNC_STATE['saved']:,} แถว"
        log(f"[Sync] เสร็จ {SYNC_STATE['saved']:,} แถว")
    except Exception as e:
        SYNC_STATE["err"] = str(e)[:200]
        SYNC_STATE["msg"] = "ไม่สำเร็จ"
        log("[X] ซิงค์ไม่สำเร็จ:", e)
    finally:
        SYNC_STATE["running"] = False


# ============================================================ HTTP
def my_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


PAGE = """<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ทะเบียนคนไข้</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:12px;background:#f7f8fa;color:#222}
h2{margin:4px 0 2px;font-size:20px}
.sub{color:#888;font-size:12px;margin-bottom:10px}
.row{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.row input[type=checkbox]{width:20px;height:20px;flex:none}
.row input[type=text],.row select{flex:1;padding:9px;border:1px solid #ccc;border-radius:6px;font-size:15px}
button{font-size:16px;padding:12px;background:#1565c0;color:#fff;border:none;border-radius:8px;width:100%}
.chip{display:inline-block;font-size:12px;padding:4px 8px;border:1px solid #ccc;border-radius:12px;background:#fff;margin:2px 2px 0 0}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th,td{padding:5px;border:1px solid #ddd}
th{background:#eef2f7}
.wrap{overflow-x:auto;margin-top:10px}
.warn{color:#d10000;font-size:13px}
.box{background:#fff;border:1px solid #e3e7ee;border-radius:10px;padding:10px;margin-bottom:10px}
</style></head><body>
<h2>&#127973; ทะเบียนคนไข้ (ข้อมูลสดจาก PC)</h2>
<div class="sub">คนไข้ <b id="tot">?</b> คน &middot; ลายนิ้วมือ <b id="fp">?</b> รายการ &middot; อ่านจาก pat4.dbf โดยตรง</div>

<div class="box" id="qbox" style="display:none;border-color:#e8b84a;background:#fffaf0">
  <div style="font-weight:bold;color:#a86b00">&#9888; มีรายการรอเขียนลง pat4.dbf: <span id="qn">0</span> รายการ</div>
  <div id="qlist" style="font-size:12px;margin:6px 0"></div>
  <div style="font-size:12px;color:#a86b00;margin-bottom:6px">
    ปิดโปรแกรม FoxPro ก่อนกดเขียน &middot; ระบบจะสำรองไฟล์ให้อัตโนมัติ</div>
  <button id="qgo" style="background:#a86b00">เขียนลง pat4.dbf</button>
  <div class="row" style="margin-top:8px">
    <input type="password" id="qpin" placeholder="PIN ของ admin (สำหรับยกเลิก)" style="max-width:210px">
    <button id="qcancelall" style="background:#a00;width:auto;padding:10px 12px;font-size:14px">ยกเลิกทั้งหมด</button>
  </div>
  <div id="qmsg" style="font-size:13px;margin-top:6px"></div>
</div>

<div class="box">
  <div class="row"><input type="checkbox" id="c_name"><input type="text" id="q_name" placeholder="ชื่อ"></div>
  <div class="row"><input type="checkbox" id="c_surname"><input type="text" id="q_surname" placeholder="นามสกุล"></div>
  <div class="row"><input type="checkbox" id="c_address"><input type="text" id="q_address" placeholder="ที่อยู่"></div>
  <div class="row"><input type="checkbox" id="c_id"><input type="text" id="q_id" placeholder="เลขบัตรประชาชน" inputmode="numeric"></div>
  <div class="row"><input type="checkbox" id="c_disease"><input type="text" id="q_disease" placeholder="โรค"></div>
  <div class="row"><input type="checkbox" id="c_number"><input type="text" id="q_number" placeholder="เลขคนไข้" inputmode="numeric"></div>
  <div class="row"><input type="checkbox" id="c_sex">
    <select id="q_sex"><option value="">เพศ: ทั้งหมด</option><option value="&#3594;">ชาย</option><option value="&#3597;">หญิง</option></select></div>
  <div class="row"><span style="font-size:14px;color:#666">ช่วงอายุ</span>
    <input type="text" id="q_age1" placeholder="เริ่ม" inputmode="numeric" style="max-width:70px">
    <span style="font-size:14px;color:#666">ถึง</span>
    <input type="text" id="q_age2" placeholder="จบ" inputmode="numeric" style="max-width:70px"></div>
  <div>
    <button class="chip" data-a="0" data-b="15">0-15</button>
    <button class="chip" data-a="16" data-b="30">16-30</button>
    <button class="chip" data-a="31" data-b="45">31-45</button>
    <button class="chip" data-a="46" data-b="60">46-60</button>
    <button class="chip" data-a="61" data-b="75">61-75</button>
    <button class="chip" data-a="76" data-b="200">76+</button>
    <button class="chip" data-a="" data-b="">ล้าง</button>
  </div>
  <div class="row" style="margin-top:8px"><input type="checkbox" id="c_nofp" style="width:20px;height:20px">
    <label for="c_nofp" style="font-size:14px">เฉพาะคนที่<b>ยังไม่เก็บ</b>ลายนิ้วมือ</label></div>
  <button id="go" style="margin-top:6px">&#128269; ค้นตามที่เลือก</button>
  <div id="warn" class="warn"></div>
</div>
<div id="result"></div>

<div class="box" id="syncbox" style="margin-top:14px">
  <div style="font-weight:bold">ซิงค์ทะเบียนคนไข้ขึ้นเว็บ (D1)</div>
  <div id="syncsum" style="font-size:13px;color:#555;margin:4px 0">กำลังโหลด...</div>
  <div class="row">
    <input type="password" id="syncpin" placeholder="PIN ของ admin" style="max-width:180px">
    <button id="syncgo" style="background:#1565c0;width:auto;padding:10px 14px;font-size:14px">ซิงค์ที่เปลี่ยน</button>
    <button id="syncall" style="background:#a86b00;width:auto;padding:10px 12px;font-size:13px">ส่งใหม่ทั้งหมด</button>
  </div>
  <div id="syncmsg" style="font-size:13px;margin-top:6px"></div>
</div>

<div class="box" id="slotbox" style="margin-top:14px">
  <div style="font-weight:bold">ช่องสแกนลายนิ้วมือ</div>
  <div id="slotsum" style="font-size:13px;color:#555;margin:4px 0">กำลังโหลด...</div>
  <div id="slotlist" style="font-size:12px"></div>
  <div id="slotmsg" style="font-size:13px;margin-top:4px"></div>
</div>

<div style="text-align:center;color:#aaa;font-size:11px;margin-top:14px">zk_server v16</div>

<div class="box" style="margin-top:16px;border-color:#e0a0a0">
  <div style="font-size:13px;color:#a00;font-weight:bold">ขั้นสูง (admin เท่านั้น)</div>
  <div class="row" style="margin-top:6px">
    <input type="password" id="wpin" placeholder="PIN ของ admin" style="max-width:150px">
    <button id="wipe" style="background:#a00;width:auto;padding:10px 14px">ลบลายนิ้วมือทั้งหมด</button>
  </div>
  <div style="font-size:11px;color:#888">ระบบจะสำรองเป็นไฟล์ zip ก่อนลบเสมอ</div>
  <div id="wmsg" style="font-size:13px;margin-top:4px"></div>
</div>

<script>
var KEYS=['name','surname','address','id','disease','number','sex'];
KEYS.forEach(function(k){
  var i=document.getElementById('q_'+k), c=document.getElementById('c_'+k);
  var ev=(i.tagName==='SELECT')?'change':'input';
  i.addEventListener(ev,function(){ if(i.value.trim()){c.checked=true;i.style.borderColor='#ccc';} });
});
[].forEach.call(document.querySelectorAll('.chip'),function(b){
  b.addEventListener('click',function(){
    document.getElementById('q_age1').value=b.getAttribute('data-a');
    document.getElementById('q_age2').value=b.getAttribute('data-b');
  });
});
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
var LAST=[], dir=0;
function render(){
  var rows=LAST.slice();
  if(dir!==0) rows.sort(function(a,b){return dir*((+a.age||0)-(+b.age||0));});
  var arrow = dir===1?'\\u25B2':(dir===-1?'\\u25BC':'\\u21C5');
  var h='<div style="color:#666;font-size:13px">พบ '+LAST.length+(LAST._capped?'+ (แสดง 200 แรก)':'')+' รายชื่อ</div>';
  h+='<div class="wrap"><table><tr><th>เลข</th><th>ชื่อ</th><th>นามสกุล</th>';
  h+='<th id="ah" style="cursor:pointer;background:#dde7f5">อายุ '+arrow+'</th>';
  h+='<th>วันที่</th><th>ที่อยู่</th><th>เลขบัตร</th><th>นิ้วที่เก็บ</th></tr>';
  for(var i=0;i<rows.length;i++){var r=rows[i];
    h+='<tr><td>'+esc(r.number)+'</td><td>'+esc(r.name)+'</td><td>'+esc(r.surname)+'</td>'
     +'<td style="text-align:center">'+esc(r.age)+'</td><td>'+esc(r.dt)+'</td>'
     +'<td>'+esc(r.address)+'</td><td>'+esc(r.id_card)+'</td>'
     +'<td style="text-align:center">'+(r.fingers&&r.fingers.length?esc(r.fingers.join('+')):'-')+'</td></tr>';
  }
  h+='</table></div>';
  document.getElementById('result').innerHTML=h;
  document.getElementById('ah').addEventListener('click',function(){dir=dir===1?-1:1;render();});
}
function go(){
  var w=document.getElementById('warn'); w.textContent='';
  KEYS.forEach(function(k){document.getElementById('q_'+k).style.borderColor='#ccc';});
  var q={};
  for(var i=0;i<KEYS.length;i++){
    var k=KEYS[i], c=document.getElementById('c_'+k), inp=document.getElementById('q_'+k);
    if(!c.checked) continue;
    if(inp.value.trim()===''){ inp.style.borderColor='#d10000'; w.textContent='ช่องที่เลือกยังว่าง'; inp.focus(); return; }
    q[k]=inp.value.trim();
  }
  var a1=document.getElementById('q_age1').value.trim(), a2=document.getElementById('q_age2').value.trim();
  if(a1)q.age1=a1; if(a2)q.age2=a2;
  if(document.getElementById('c_nofp').checked) q.nofp='1';
  if(!Object.keys(q).length){ w.textContent='เลือกและกรอกอย่างน้อย 1 ช่อง'; return; }
  document.getElementById('result').textContent='กำลังค้น...';
  fetch('/websearch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(q)})
   .then(function(r){return r.json();})
   .then(function(j){
     if(!j.ok){document.getElementById('result').innerHTML='<span class="warn">'+esc(j.err)+'</span>';return;}
     if(!j.rows.length){document.getElementById('result').innerHTML='<span style="color:#888">ไม่พบข้อมูล</span>';return;}
     LAST=j.rows; LAST._capped=j.capped; dir=0; render();
   })
   .catch(function(e){document.getElementById('result').innerHTML='<span class="warn">'+e+'</span>';});
}
document.getElementById('go').addEventListener('click',go);
fetch('/ping').then(function(r){return r.json();}).then(function(j){
  document.getElementById('tot').textContent=(j.patients||0).toLocaleString();
  document.getElementById('fp').textContent=(j.fingers||0).toLocaleString();
}).catch(function(){});

function loadQueue(){
  fetch('/webqueue',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(function(r){return r.json();})
   .then(function(j){
     if(!j.ok||!j.n){document.getElementById('qbox').style.display='none';return;}
     document.getElementById('qbox').style.display='block';
     document.getElementById('qn').textContent=j.n;
     var h='';
     for(var i=0;i<j.rows.length;i++){var q=j.rows[i];
       h+='<div style="margin-bottom:4px">&bull; '+(q.kind==='add'?'เพิ่ม':'แก้ ลำดับ '+q.rid)+': <b>'+esc(q.name)+'</b>'
        +' <span style="color:#888">('+esc(q.fields)+')</span>'
        +(q.hasfp?' <span style="color:#0a7a0a">+ลายนิ้วมือ</span>':'')
        +' <span style="color:#aaa">'+esc(q.ts)+'</span>'
        +' <button class="qdel" data-id="'+q.id+'" data-fp="'+(q.hasfp?1:0)+'"'
        +' style="background:#fff;color:#a00;border:1px solid #a00;width:auto;padding:2px 8px;font-size:12px">ยกเลิก</button>'
        +'</div>';
     }
     document.getElementById('qlist').innerHTML=h;
     [].forEach.call(document.querySelectorAll('.qdel'),function(b){
       b.addEventListener('click',function(){ cancelQ(b.getAttribute('data-id'), b.getAttribute('data-fp')==='1'); });
     });
   }).catch(function(){});
}

function cancelQ(id, hasfp){
  var pin=document.getElementById('qpin').value.trim();
  var m=document.getElementById('qmsg');
  if(!pin){m.style.color='#d10000';m.textContent='ใส่ PIN ของ admin ก่อนยกเลิก';return;}
  var warn = hasfp ? '\\n\\nรายการนี้มีลายนิ้วมือแนบอยู่ - ยกเลิกแล้วต้องเก็บใหม่' : '';
  if(!confirm('ยกเลิกรายการนี้ใช่ไหม'+warn))return;
  var body = (id===null) ? {pin:pin} : {pin:pin, id:Number(id)};
  fetch('/webcancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(function(r){return r.json();})
   .then(function(j){
     if(j.ok){
       m.style.color='#0a7a0a';
       m.textContent='ยกเลิกแล้ว '+j.removed+' รายการ'
                   +(j.fp_lost?' (ลายนิ้วมือหายไป '+j.fp_lost+')':'')
                   +' · เหลือในคิว '+j.left;
       loadQueue();
     } else { m.style.color='#d10000'; m.textContent=j.err; }
   }).catch(function(e){m.style.color='#d10000';m.textContent=''+e;});
}
document.getElementById('qgo').addEventListener('click',function(){
  if(!confirm('ปิดโปรแกรม FoxPro แล้วใช่ไหม? ระบบจะสำรองไฟล์ก่อนเขียน'))return;
  var b=this; b.disabled=true;
  document.getElementById('qmsg').textContent='กำลังเขียน...';
  fetch('/webapply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(function(r){return r.json();})
   .then(function(j){
     b.disabled=false;
     var m=document.getElementById('qmsg');
     if(j.ok){
       m.style.color='#0a7a0a';
       m.textContent='เขียนสำเร็จ - เพิ่ม '+j.added+' · แก้ '+j.edited
                   +(j.linked?' · ผูกลายนิ้วมือ '+j.linked:'')+' · คนไข้รวม '+j.patients;
       loadQueue();
       fetch('/ping').then(function(r){return r.json();}).then(function(p){
         document.getElementById('tot').textContent=(p.patients||0).toLocaleString();});
     } else {
       m.style.color='#d10000'; m.textContent='ไม่สำเร็จ: '+j.err;
     }
   }).catch(function(e){b.disabled=false;document.getElementById('qmsg').textContent=''+e;});
});
document.getElementById('qcancelall').addEventListener('click',function(){
  var n=document.getElementById('qn').textContent;
  if(!confirm('ยกเลิกคิวทั้งหมด '+n+' รายการใช่ไหม'))return;
  if(!confirm('ยืนยันอีกครั้ง - รายการที่ยังไม่ได้เขียนลง dbf จะหายทั้งหมด'))return;
  cancelQ(null, false);
});

function loadSlots(){
  fetch('/webslots',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(function(r){return r.json();})
   .then(function(j){
     if(!j.ok)return;
     document.getElementById('slotsum').textContent =
       'ใช้แล้ว '+j.n_used+' ช่อง · มีปัญหา '+j.n_bad+' ช่อง · ว่าง '+j.free.toLocaleString()+' / '+j.max.toLocaleString();
     var h='';
     var ks=Object.keys(j.used).sort(function(a,b){return a-b;});
     for(var i=0;i<Math.min(ks.length,50);i++){
       var k=ks[i], v=j.used[k];
       h+='<div>ช่อง '+k+' &nbsp; <b>'+esc(v.name)+'</b> &middot; '
        +esc(v.finger==='R2'?'นิ้วชี้ขวา':'นิ้วหัวแม่มือขวา')
        +' <span style="color:#aaa">HN '+esc(String(v.number))+'</span></div>';
     }
     if(ks.length>50) h+='<div style="color:#888">... และอีก '+(ks.length-50)+' ช่อง</div>';
     var bk=Object.keys(j.bad).sort(function(a,b){return a-b;});
     if(bk.length){
       h+='<div style="margin-top:6px;color:#a86b00;font-weight:bold">ช่องที่มีปัญหา (ระบบไม่แจกซ้ำ)</div>';
       for(var i=0;i<bk.length;i++){
         var k=bk[i], v=j.bad[k];
         h+='<div>ช่อง '+k+' <span style="color:#888">'+esc(v.why||'')+' &middot; '+esc(v.found||'')+'</span>'
          +' <button class="slotc" data-id="'+k+'" style="background:#fff;color:#a00;border:1px solid #a00;'
          +'width:auto;padding:2px 8px;font-size:12px">ล้าง</button></div>';
       }
     }
     document.getElementById('slotlist').innerHTML=h;
     [].forEach.call(document.querySelectorAll('.slotc'),function(b){
       b.addEventListener('click',function(){ clearSlot(b.getAttribute('data-id')); });
     });
   }).catch(function(){});
}

function clearSlot(id){
  var pin=document.getElementById('wpin').value.trim();
  var m=document.getElementById('slotmsg');
  if(!pin){m.style.color='#d10000';m.textContent='ใส่ PIN ในกล่องขั้นสูงด้านล่างก่อน';return;}
  if(!confirm('ล้างช่อง '+id+' ให้กลับมาใช้ได้ใช่ไหม'))return;
  fetch('/slotclear',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pin:pin,secret:'',sid:Number(id)})})
   .then(function(r){return r.json();})
   .then(function(j){
     if(j.ok){m.style.color='#0a7a0a';m.textContent='ล้างแล้ว · เหลือช่องมีปัญหา '+j.n_bad;loadSlots();}
     else {m.style.color='#d10000';m.textContent=j.err;}
   }).catch(function(e){m.textContent=''+e;});
}

function loadSync(){
  fetch('/syncstat',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(function(r){return r.json();})
   .then(function(j){
     if(!j.ok)return;
     var t='ซิงค์แล้ว '+(j.synced||0).toLocaleString()+' แถว';
     if(j.pending) t+=' · <b style="color:#a86b00">รอส่ง '+j.pending.toLocaleString()+'</b>';
     else t+=' · <span style="color:#0a7a0a">ตรงกันแล้ว</span>';
     if(j.nouid) t+=' · <span style="color:#d10000">ยังไม่มี HN_UID '+j.nouid.toLocaleString()+'</span>';
     document.getElementById('syncsum').innerHTML=t;
     var m=document.getElementById('syncmsg');
     if(j.running){
       m.style.color='#1565c0';
       m.textContent=j.msg+(j.total?(' ('+Math.round(j.done*100/j.total)+'%)'):'');
       setTimeout(loadSync,2000);
     } else if(j.err){ m.style.color='#d10000'; m.textContent='ไม่สำเร็จ: '+j.err; }
     else if(j.msg){ m.style.color='#0a7a0a'; m.textContent=j.msg; }
   }).catch(function(){});
}

function startSync(full){
  var pin=document.getElementById('syncpin').value.trim();
  var m=document.getElementById('syncmsg');
  if(!pin){m.style.color='#d10000';m.textContent='ใส่ PIN ของ admin ก่อน';return;}
  if(full && !confirm('ส่งข้อมูลทั้งหมดขึ้นใหม่ (ใช้เวลานาน) ใช่ไหม'))return;
  m.style.color='#1565c0';m.textContent='กำลังเริ่ม...';
  fetch('/websync',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pin:pin,full:!!full})})
   .then(function(r){return r.json();})
   .then(function(j){
     if(j.ok){ setTimeout(loadSync,800); }
     else { m.style.color='#d10000'; m.textContent=j.err; }
   }).catch(function(e){m.style.color='#d10000';m.textContent=''+e;});
}

document.getElementById('syncgo').addEventListener('click',function(){ startSync(false); });
document.getElementById('syncall').addEventListener('click',function(){ startSync(true); });

loadQueue();
loadSlots();
loadSync();
setInterval(loadQueue, 30000);
setInterval(loadSlots, 60000);

document.getElementById('wipe').addEventListener('click',function(){
  var pin=document.getElementById('wpin').value.trim();
  if(!pin){document.getElementById('wmsg').textContent='ใส่ PIN ของ admin';return;}
  if(!confirm('ลบลายนิ้วมือทั้งหมดใช่ไหม? (สำรอง zip ให้ก่อน)'))return;
  if(!confirm('ยืนยันอีกครั้ง - ลบแล้วต้องกู้จากไฟล์ zip เท่านั้น'))return;
  var b=this;b.disabled=true;
  document.getElementById('wmsg').textContent='กำลังลบ...';
  fetch('/webwipefp',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pin:pin})})
   .then(function(r){return r.json();})
   .then(function(j){
     b.disabled=false;
     var m=document.getElementById('wmsg');
     if(j.ok){m.style.color='#0a7a0a';
       m.textContent='ลบแล้ว '+j.removed_people+' คน / '+j.removed_fingers+' นิ้ว · สำรองไว้ที่ '+j.backup;
       document.getElementById('fp').textContent='0';
     } else {m.style.color='#d10000';m.textContent=j.err;}
   }).catch(function(e){b.disabled=false;document.getElementById('wmsg').textContent=''+e;});
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_one_request(self):
        """ซ่อน ConnectionResetError - เบราว์เซอร์ปิดการเชื่อมต่อเป็นเรื่องปกติ ไม่ใช่ข้อผิดพลาด"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception:
            self.close_connection = True

    def _ip(self):
        return self.client_address[0]

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, s):
        body = s.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            return {}
        if n <= 0 or n > 60 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._html(PAGE)
        if self.path.startswith("/ping"):
            n, sz = img_count_size()
            return self._json({
                "ok": True,
                "patients": len(PAT),
                "fingers": fp_count(),
                "people": len(FP),
                "images": n,
                "images_mb": round(sz / 1024 / 1024, 1),
                "queue": len(QUEUE),
                "mode": "phone-match",
                "msg": "พร้อมใช้งาน (มือถือเป็นคนเทียบลายนิ้วมือ)",
            })
        return self._json({"ok": False, "err": "ไม่รู้จักคำสั่งนี้"}, 404)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        ip = self._ip()
        b = self._body()

        # ---- ค้นหาจากหน้าเว็บ (ในวง LAN ไม่ต้องใส่รหัส) ----
        if self.path.startswith("/websearch"):
            return self._search(b, ip, "web", limit=200)

        # ---- ดูความคืบหน้าการซิงค์ ----
        if self.path.startswith("/syncstat"):
            st = dict(SYNC_STATE)
            st["ok"] = True
            st["pending"] = 0
            if st.get("running"):
                # กำลังซิงค์อยู่ - ไม่ต้องนับใหม่ (หนักเปล่าๆ) ใช้ตัวเลขจากงานที่ทำอยู่
                st["pending"] = max(0, int(st.get("total") or 0) - int(st.get("done") or 0))
                st["synced"] = SYNC_COUNT.get("n", 0)
                st["nouid"] = SYNC_COUNT.get("nouid", 0)
                return self._json(st)
            try:
                old_sig = load_sync()
                n = 0
                nouid = 0
                for p2 in PAT:
                    u = (p2.get("hn_uid") or "").strip()
                    if not u:
                        nouid += 1
                        continue
                    if old_sig.get(u) != row_sig(p2):
                        n += 1
                st["pending"] = n
                st["nouid"] = nouid
                st["synced"] = len(old_sig)
                SYNC_COUNT["n"] = len(old_sig)
                SYNC_COUNT["nouid"] = nouid
            except Exception:
                pass
            return self._json(st)

        # ---- เริ่มซิงค์ขึ้น D1 (admin เท่านั้น) ----
        if self.path.startswith("/websync"):
            pin = str(b.get("pin", "")).strip()
            ok_admin = (pin == ADMIN_PIN) or ((ask_worker(pin) or {}) or {}).get("role") == "admin"
            if not ok_admin:
                return self._json({"ok": False, "err": "ต้องใส่ PIN ของ admin"}, 403)
            if SYNC_STATE["running"]:
                return self._json({"ok": False, "err": "กำลังซิงค์อยู่แล้ว"})
            full = bool(b.get("full"))
            threading.Thread(target=do_sync, args=(pin, full), daemon=True).start()
            audit(ip, "web-admin", "sync", "full" if full else "changed")
            return self._json({"ok": True, "started": True})

        # ---- แผนที่ช่องสแกน สำหรับหน้าเว็บ (ในวง LAN ไม่ต้องใส่รหัส) ----
        if self.path.startswith("/webslots"):
            return self._json(slot_map())

        # ---- ดูคิวจากหน้าเว็บ ----
        if self.path.startswith("/webqueue"):
            rows = []
            for it in QUEUE[:50]:
                p2 = find_patient(it.get("rid") or 0) if it.get("kind") == "edit" else None
                rows.append({
                    "id": it["id"], "kind": it["kind"], "rid": it.get("rid"),
                    "ts": it["ts"], "who": it.get("who", ""),
                    "name": (it["data"].get("NAME") or "") if it["kind"] == "add"
                            else (p2["name"] if p2 else "?"),
                    "fields": ", ".join(sorted(it["data"].keys())),
                    "hasfp": bool(it.get("fp")),
                })
            return self._json({"ok": True, "n": len(QUEUE), "rows": rows})

        # ---- ลบลายนิ้วมือทั้งหมด (หน้าเว็บ PC) ----
        if self.path.startswith("/webwipefp"):
            if str(b.get("pin", "")) != ADMIN_PIN and not (
                    ask_worker(str(b.get("pin", ""))) or {}).get("role") == "admin":
                return self._json({"ok": False, "err": "ต้องใส่ PIN ของ admin"}, 403)
            n0, f0 = len(FP), fp_count()
            # สำรองเป็น zip ก่อนลบ
            zpath = ""
            try:
                import zipfile
                zpath = os.path.join(HERE, "fp_backup_" + time.strftime("%Y%m%d_%H%M%S") + ".zip")
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                    if os.path.exists(TPL_PATH):
                        z.write(TPL_PATH, os.path.basename(TPL_PATH))
                    if os.path.isdir(IMG_DIR):
                        for fn in os.listdir(IMG_DIR):
                            z.write(os.path.join(IMG_DIR, fn), "fp_images/" + fn)
            except Exception as e:
                return self._json({"ok": False, "err": "สำรองไม่สำเร็จ จึงไม่ลบ: " + str(e)[:100]})
            FP.clear()
            SID_USED.clear()
            SID_HOLD.clear()
            SID_BAD.clear()
            try:
                save_slots()
            except Exception:
                pass
            try:
                save_fingers()
                if os.path.isdir(IMG_DIR):
                    for fn in os.listdir(IMG_DIR):
                        os.remove(os.path.join(IMG_DIR, fn))
            except Exception as e:
                return self._json({"ok": False, "err": str(e)[:120]})
            log(f"[!] ลบลายนิ้วมือทั้งหมด ({n0} คน / {f0} นิ้ว) - สำรองไว้ที่ {os.path.basename(zpath)}")
            audit(ip, "web-admin", "wipe-fp", f"{n0} คน {f0} นิ้ว -> {os.path.basename(zpath)}")
            return self._json({"ok": True, "removed_people": n0, "removed_fingers": f0,
                               "backup": os.path.basename(zpath)})

        # ---- ยกเลิกรายการในคิว (admin เท่านั้น) ----
        if self.path.startswith("/webcancel"):
            pin = str(b.get("pin", "")).strip()
            ok_admin = (pin == ADMIN_PIN) or ((ask_worker(pin) or {}) or {}).get("role") == "admin"
            if not ok_admin:
                return self._json({"ok": False, "err": "ต้องใส่ PIN ของ admin"}, 403)
            if not QUEUE:
                return self._json({"ok": False, "err": "ไม่มีรายการในคิว"})

            qid = b.get("id")
            if qid is None:                       # ยกเลิกทั้งหมด
                n = len(QUEUE)
                nfp = sum(1 for x in QUEUE if x.get("fp"))
                QUEUE[:] = []
                save_queue()
                log(f"[!] ยกเลิกคิวทั้งหมด {n} รายการ" + (f" (มีลายนิ้วมือ {nfp})" if nfp else ""))
                audit(ip, "web-admin", "cancel-all", f"{n} รายการ")
                return self._json({"ok": True, "removed": n, "fp_lost": nfp, "left": 0})

            try:
                qid = int(qid)
            except Exception:
                return self._json({"ok": False, "err": "id ไม่ถูกต้อง"})
            for i, it in enumerate(QUEUE):
                if it.get("id") == qid:
                    gone = QUEUE.pop(i)
                    save_queue()
                    who = (gone["data"].get("NAME") or "") if gone["kind"] == "add" else f"rid={gone.get('rid')}"
                    log(f"[!] ยกเลิกคิว: {gone['kind']} {who}"
                        + (" (ลายนิ้วมือที่แนบหายไปด้วย)" if gone.get("fp") else ""))
                    audit(ip, "web-admin", "cancel-one", f"{gone['kind']} {who}")
                    return self._json({"ok": True, "removed": 1,
                                       "fp_lost": 1 if gone.get("fp") else 0,
                                       "left": len(QUEUE)})
            return self._json({"ok": False, "err": "ไม่พบรายการนี้ (อาจถูกเขียนไปแล้ว)"})

        # ---- เขียนคิวลง dbf (จากหน้าเว็บ PC) ----
        if self.path.startswith("/webapply"):
            if not QUEUE:
                return self._json({"ok": False, "err": "ไม่มีรายการในคิว"})
            try:
                import dbfwrite
            except Exception as e:
                return self._json({"ok": False, "err": "ไม่พบไฟล์ dbfwrite.py (" + str(e)[:80] + ")"})
            msgs = []
            r = dbfwrite.apply_queue(DBF_PATH, QUEUE, log=lambda *a: msgs.append(" ".join(str(x) for x in a)))
            if not r.get("ok"):
                log("[X] เขียน dbf ไม่สำเร็จ:", r.get("err"))
                audit(ip, "web", "apply-fail", str(r.get("err"))[:80])
                return self._json({"ok": False, "err": r.get("err"), "msgs": msgs})

            # ผูกลายนิ้วมือที่รอไว้ เข้ากับแถวใหม่ที่เพิ่งเกิด
            linked = 0
            for idx, it in enumerate(QUEUE):
                if it.get("kind") == "add" and it.get("fp"):
                    rid = (r.get("newrids") or {}).get(str(idx))
                    if rid:
                        rid = int(rid)
                        p2 = find_patient(rid)
                        uid2 = str((p2 or {}).get("hn_uid") or "").strip()
                        if not uid2:
                            log("[!] แถวใหม่ยังไม่มี HN_UID - ลายนิ้วมือยังไม่ผูก "
                                "(รัน add_hnuid.prg ที่ FoxPro แล้วเก็บใหม่)")
                            continue
                        fg2 = str(it.get("fpfinger") or "R1").upper()
                        with SID_LOCK:
                            sid2 = next_sid()
                            if sid2 > 0:
                                SID_USED[sid2] = (uid2, fg2)
                        if sid2 <= 0:
                            log("[!] ช่องสแกนเต็ม - ลายนิ้วมือยังไม่ผูก")
                            continue
                        FP[uid2] = {"uid": uid2,
                                    "name": (p2 or {}).get("name", ""),
                                    "number": (p2 or {}).get("number", ""),
                                    "f": {fg2: {"t": it["fp"], "ts": time.time(),
                                                "img": "", "sid": sid2}}}
                        linked += 1
            if linked:
                try:
                    save_fingers()
                except Exception:
                    pass

            QUEUE[:] = []
            save_queue()
            load_dbf(force=True)          # โหลดทะเบียนใหม่ทันที
            log(f"[OK] เขียน dbf แล้ว: เพิ่ม {r['added']} · แก้ {r['edited']} · ผูกลายนิ้วมือ {linked}")
            audit(ip, "web", "apply-ok", f"add={r['added']} edit={r['edited']}")
            return self._json({"ok": True, "added": r["added"], "edited": r["edited"],
                               "linked": linked, "patients": len(PAT), "msgs": msgs})

        # ---- ที่เหลือต้องผ่าน SECRET + PIN ----
        user, err = check_auth(b, ip)
        if err:
            audit(ip, str(b.get("dev", ""))[:20], "auth-fail")
            return self._json({"ok": False, "err": err}, 403)
        dev = user["name"]                    # ใช้ชื่อคนจริงใน log
        role = user.get("role", "worker")

        # ---- ตรวจว่าใช่ตัวจริงไหม (ให้แอปเช็คก่อนใช้งาน) ----
        if self.path.startswith("/hello"):
            return self._json({"ok": True, "name": user["name"], "role": role,
                               "patients": len(PAT), "fingers": fp_count(),
                               "people": len(FP), "queue": len(QUEUE),
                               "fingers_per_person": FINGERS,
                               "sid_used": len(SID_USED), "sid_bad": len(SID_BAD), "sid_max": SID_MAX,
                               "has_hnuid": bool(PAT and PAT[0].get("hn_uid"))})

        if self.path.startswith("/search"):
            return self._search(b, ip, dev, limit=100)

        # ---- มือถือขอ template ไปเทียบเอง ----
        if self.path.startswith("/templates"):
            since = float(b.get("since") or 0)
            out = []
            for uid, v in FP.items():
                for fg, d in (v.get("f") or {}).items():
                    try:
                        sid = int(d.get("sid") or 0)
                    except Exception:
                        sid = 0
                    if sid <= 0 or not d.get("t"):
                        continue
                    if float(d.get("ts") or 0) > since:
                        out.append({"sid": sid, "uid": uid, "finger": fg, "t": d["t"]})
            audit(ip, dev, "templates", f"ส่ง {len(out)} นิ้ว")
            return self._json({"ok": True, "rows": out, "people": len(FP),
                               "total": fp_count(), "now": time.time(),
                               "used": len(SID_USED), "max": SID_MAX})

        # ---- มือถือเทียบเจอแล้ว ขอข้อมูลคนไข้ ----
        if self.path.startswith("/patient"):
            uid = str(b.get("uid") or "").strip()
            try:
                sid = int(b.get("sid") or 0)
            except Exception:
                sid = 0
            fg_found = ""
            if not uid and sid > 0:
                got = SID_USED.get(sid)
                if got:
                    uid, fg_found = got[0], got[1]
            p = find_by_uid(uid) if uid else None
            if not p:
                try:
                    rid = int(b.get("rid") or 0)
                except Exception:
                    rid = 0
                if rid > 0:
                    p = find_patient(rid)
            audit(ip, dev, "patient", f"sid={sid} uid={uid} {'พบ' if p else 'ไม่พบ'}")
            if not p:
                return self._json({"ok": True, "found": False,
                                   "err": "ไม่พบคนไข้ (อาจถูกลบไปแล้ว)"})
            rec = FP.get(uid) or {}
            have = sorted((rec.get("f") or {}).keys())
            warn = ""
            if rec.get("name") and rec.get("name") != p["name"]:
                warn = f"ชื่อไม่ตรงกับตอนเก็บลายนิ้วมือ (เคยเป็น {rec.get('name')})"
            return self._json({"ok": True, "found": True, "patient": p, "warn": warn,
                               "uid": uid, "sid": sid, "finger": fg_found, "have": have})

        # ---- ขอช่องสแกน (scanner_id) ก่อน Register ----
        if self.path.startswith("/sid"):
            try:
                rid = int(b.get("rid") or 0)
            except Exception:
                rid = 0
            uid = str(b.get("uid") or "").strip()
            fg = str(b.get("finger") or "R1").upper()
            if fg not in FINGERS:
                return self._json({"ok": False, "err": f"นิ้วต้องเป็น {' หรือ '.join(FINGERS)}"})
            p = find_by_uid(uid) if uid else None
            if not p and rid > 0:
                p = find_patient(rid)
                uid = str(p.get("hn_uid") or "").strip() if p else ""
            if not p:
                return self._json({"ok": False, "err": "ไม่พบคนไข้"})
            if not uid:
                return self._json({"ok": False,
                                   "err": "คนไข้คนนี้ยังไม่มี HN_UID - รัน add_hnuid.prg ที่ FoxPro ก่อน"})
            # มือถือแจ้งว่าช่องไหนไม่ว่างจริง (lazy check) -> จดไว้ ไม่แจกซ้ำ
            av = b.get("avoid")
            if isinstance(av, list):
                for x in av[:20]:
                    try:
                        n = int(x)
                    except Exception:
                        continue
                    if n > 0 and n not in SID_BAD:
                        mark_bad(n, "มือถือพบว่ามีข้อมูลอยู่ในเครื่องสแกน แต่ PC ไม่รู้จัก", dev)
                        log(f"[!] ช่อง {n} ไม่ว่างจริง - จดไว้แล้ว ไม่แจกซ้ำ")
            sid, how = hold_sid(uid, fg)
            if sid <= 0:
                return self._json({"ok": False,
                                   "err": f"ช่องสแกนเต็ม ({SID_MAX:,}) - ลบของเก่าก่อน"})
            rec = FP.get(uid) or {}
            have = sorted((rec.get("f") or {}).keys())
            sids = {}
            for k, d in (rec.get("f") or {}).items():
                sids[k] = d.get("sid", 0)
            audit(ip, dev, "sid", f"{uid} {fg} -> {sid} ({how})")
            return self._json({"ok": True, "sid": sid, "uid": uid, "finger": fg, "how": how,
                               "patient": p, "have": have, "sids": sids,
                               "need": [x for x in FINGERS if x not in have],
                               "used": len(SID_USED), "max": SID_MAX})

        # ---- เก็บลายนิ้วมือ (template + ภาพ) ----
        if self.path.startswith("/enroll"):
            uid = str(b.get("uid") or "").strip()
            try:
                rid = int(b.get("rid") or 0)
            except Exception:
                rid = 0
            tpl = str(b.get("template") or "")
            fg = str(b.get("finger") or "R1").upper()
            if fg not in FINGERS:
                return self._json({"ok": False, "err": f"นิ้วต้องเป็น {' หรือ '.join(FINGERS)}"})
            if not tpl:
                return self._json({"ok": False, "err": "ไม่มี template"})

            p = find_by_uid(uid) if uid else None
            if not p and rid > 0:
                p = find_patient(rid)
                uid = str(p.get("hn_uid") or "").strip() if p else ""
            if not p:
                return self._json({"ok": False, "err": "ไม่พบคนไข้"})
            if not uid:
                return self._json({"ok": False,
                                   "err": "คนไข้คนนี้ยังไม่มี HN_UID - รัน add_hnuid.prg ก่อน"})
            try:
                base64.b64decode(tpl)
            except Exception:
                return self._json({"ok": False, "err": "template ไม่ถูกต้อง"})

            with SID_LOCK:
                rec = FP.get(uid) or {"f": {}}
                rec.setdefault("f", {})
                sid = 0
                old = rec["f"].get(fg) or {}
                try:
                    sid = int(old.get("sid") or 0)
                except Exception:
                    sid = 0
                if sid <= 0:
                    try:
                        want = int(b.get("sid") or 0)
                    except Exception:
                        want = 0
                    h = SID_HOLD.get(want)
                    if want > 0 and h and h[0] == uid and h[1] == fg and h[2] > time.time():
                        sid = want
                    else:
                        sid = next_sid()
                    if sid <= 0:
                        return self._json({"ok": False, "err": f"ช่องสแกนเต็ม ({SID_MAX:,})"})
                rec["uid"] = uid
                rec["name"] = p["name"]
                rec["number"] = p["number"]
                SID_USED[sid] = (uid, fg)
                SID_HOLD.pop(sid, None)

            # ภาพ - ชื่อ <HN_UID>_<นิ้ว>.png
            img_name = ""
            img = b.get("image")
            if img:
                try:
                    os.makedirs(IMG_DIR, exist_ok=True)
                    raw = base64.b64decode(img)
                    if len(raw) < 20 * 1024 * 1024:
                        img_name = f"{uid}_{fg}.png"
                        with open(os.path.join(IMG_DIR, img_name), "wb") as f:
                            f.write(raw)
                except Exception as e:
                    log("[!] เก็บภาพไม่สำเร็จ:", e)
                    img_name = ""

            rec["f"][fg] = {"t": tpl, "ts": time.time(), "img": img_name, "sid": sid}
            FP[uid] = rec
            try:
                save_fingers()
            except Exception as e:
                return self._json({"ok": False, "err": "บันทึกไม่ได้: " + str(e)[:120]})

            have = sorted(rec["f"].keys())
            sids = {}
            for k, d in rec["f"].items():
                sids[k] = d.get("sid", 0)
            log(f"เก็บ: ช่อง {sid} {fg} -> {p['name']} {p['surname']} "
                f"(มี {'+'.join(have)}{', มีภาพ' if img_name else ''})")
            audit(ip, dev, "enroll", f"sid={sid} {uid} {fg} img={bool(img_name)}")
            return self._json({"ok": True, "patient": p, "uid": uid, "sid": sid, "finger": fg,
                               "have": have, "sids": sids,
                               "need": [x for x in FINGERS if x not in have],
                               "people": len(FP), "total": fp_count(),
                               "used": len(SID_USED), "max": SID_MAX})

        # ---- แผนที่ช่องสแกน (ให้แอปเทียบตอนสแกน / หน้าเว็บแสดง) ----
        if self.path.startswith("/slots"):
            return self._json(slot_map())

        # ---- จดว่าช่องนี้มีปัญหา ----
        if self.path.startswith("/slotmark"):
            try:
                sid = int(b.get("sid") or 0)
            except Exception:
                sid = 0
            if sid <= 0:
                return self._json({"ok": False, "err": "ต้องระบุเลขช่อง"})
            if sid in SID_USED:
                return self._json({"ok": False, "err": f"ช่อง {sid} มีคนใช้อยู่ ไม่ควรจดว่ามีปัญหา"})
            mark_bad(sid, str(b.get("why") or "แอปแจ้งว่าไม่ว่าง"), dev)
            audit(ip, dev, "slot-mark", f"sid={sid}")
            return self._json({"ok": True, "n_bad": len(SID_BAD)})

        # ---- เคลียร์ช่องที่มีปัญหา (trusted ขึ้นไป) ----
        if self.path.startswith("/slotclear"):
            if not need_role(user, "trusted"):
                return self._json({"ok": False, "err": "เคลียร์ได้เฉพาะหัวหน้า/admin"}, 403)
            sid = b.get("sid")
            if sid is None:
                n = len(SID_BAD)
                SID_BAD.clear()
                save_slots()
                log(f"[OK] เคลียร์ช่องที่มีปัญหาทั้งหมด {n} ช่อง")
                audit(ip, dev, "slot-clear", f"ทั้งหมด {n}")
                return self._json({"ok": True, "cleared": n, "n_bad": 0})
            try:
                sid = int(sid)
            except Exception:
                return self._json({"ok": False, "err": "เลขช่องไม่ถูกต้อง"})
            if SID_BAD.pop(sid, None) is None:
                return self._json({"ok": False, "err": f"ช่อง {sid} ไม่ได้อยู่ในรายการมีปัญหา"})
            save_slots()
            log(f"[OK] เคลียร์ช่อง {sid}")
            audit(ip, dev, "slot-clear", f"sid={sid}")
            return self._json({"ok": True, "cleared": 1, "n_bad": len(SID_BAD)})

        # ---- ขอลิงก์เปิด hn-photo (PC สร้างให้ CF_SECRET ไม่ออกจากเครื่อง) ----
        if self.path.startswith("/photolink"):
            if not CF_SECRET or not WORKER:
                return self._json({"ok": False, "err": "ยังไม่ได้ตั้ง CF_SECRET/WORKER ที่ PC"})
            import urllib.parse
            to = str(b.get("to") or "/uploader")
            if not to.startswith("/"):
                to = "/uploader"
            pin = str(b.get("pin", "")).strip()
            url = (f"https://{WORKER}/applogin"
                   f"?secret={urllib.parse.quote(CF_SECRET)}"
                   f"&pin={urllib.parse.quote(pin)}"
                   f"&to={urllib.parse.quote(to)}")
            audit(ip, dev, "photolink", to)
            return self._json({"ok": True, "url": url})

        # ---- C: เพิ่มรายชื่อใหม่ (เข้าคิว) ----
        if self.path.startswith("/add"):
            d = b.get("data") or {}
            if not str(d.get("NAME", "")).strip():
                return self._json({"ok": False, "err": "ต้องมีชื่อ"})
            it = queue_add("add", d, dev, fp=b.get("template"))
            if b.get("template"):
                it["fpfinger"] = str(b.get("finger") or "R1").upper()
                save_queue()
            log(f"คิว+: เพิ่ม {d.get('NAME')} {d.get('SURNAME','')} (คิวรวม {len(QUEUE)})")
            audit(ip, dev, "queue-add", str(d.get("NAME"))[:30])
            return self._json({"ok": True, "queued": len(QUEUE), "id": it["id"]})

        # ---- D: แก้ไขข้อมูล (เข้าคิว) ----
        if self.path.startswith("/edit"):
            try:
                rid = int(b.get("rid") or 0)
            except Exception:
                rid = 0
            p = find_patient(rid)
            if not p:
                return self._json({"ok": False, "err": f"ไม่พบคนไข้ลำดับ {rid}"})
            d = b.get("data") or {}
            if not d:
                return self._json({"ok": False, "err": "ไม่มีข้อมูลที่จะแก้"})
            it = queue_add("edit", d, dev, rid=rid)
            log(f"คิว+: แก้ rid={rid} {p['name']} (คิวรวม {len(QUEUE)})")
            audit(ip, dev, "queue-edit", f"rid={rid}")
            return self._json({"ok": True, "queued": len(QUEUE), "id": it["id"]})

        # ---- D: ลบลายนิ้วมือ (ทำทันที ไม่เข้าคิว) ----
        if self.path.startswith("/delfp"):
            if not need_role(user, "trusted"):
                return self._json({"ok": False, "err": "ลบได้เฉพาะหัวหน้า/admin"}, 403)
            uid = str(b.get("uid") or "").strip()
            if not uid:
                try:
                    rid = int(b.get("rid") or 0)
                except Exception:
                    rid = 0
                uid = uid_of(rid) if rid > 0 else ""
            fg = str(b.get("finger") or "").upper()      # ว่าง = ลบทุกนิ้ว
            rec = FP.get(uid)
            if not rec:
                return self._json({"ok": False, "err": "คนนี้ยังไม่มีลายนิ้วมือ"})

            gone = []
            freed = []
            for k in (list(rec.get("f") or {}) if not fg else [fg]):
                d = (rec.get("f") or {}).pop(k, None)
                if d:
                    gone.append(k)
                    with SID_LOCK:                  # คืนช่องของนิ้วนั้นทันที
                        try:
                            sd = int(d.get("sid") or 0)
                        except Exception:
                            sd = 0
                        if sd > 0 and (SID_USED.get(sd) or ("", ""))[0] == uid:
                            SID_USED.pop(sd, None)
                            freed.append(sd)
                    nm = d.get("img")
                    if nm:
                        try:
                            fpth = os.path.join(IMG_DIR, nm)
                            if os.path.exists(fpth):
                                os.remove(fpth)
                        except Exception:
                            pass
            if not gone:
                return self._json({"ok": False, "err": f"ไม่พบนิ้ว {fg}"})
            if not rec.get("f"):
                FP.pop(uid, None)
            try:
                save_fingers()
            except Exception as e:
                return self._json({"ok": False, "err": "บันทึกไม่ได้: " + str(e)[:120]})
            p = find_by_uid(uid)
            log(f"ลบลายนิ้วมือ: {'+'.join(gone)} {p['name'] if p else uid}"
                + (f" (คืนช่อง {freed})" if freed else "")
                + f" - เหลือ {fp_count()} นิ้ว")
            audit(ip, dev, "delete-fp", f"{uid} {'+'.join(gone)} freed={freed}")
            return self._json({"ok": True, "deleted": gone, "freed_sid": freed,
                               "people": len(FP), "total": fp_count(),
                               "used": len(SID_USED), "max": SID_MAX})

        # ---- ดูคิว ----
        if self.path.startswith("/queue"):
            return self._json({"ok": True, "n": len(QUEUE), "rows": QUEUE[:50]})

        return self._json({"ok": False, "err": "ไม่รู้จักคำสั่งนี้"}, 404)

    # ---------------------------------------------------------------- ค้นหา
    def _search(self, b, ip, dev, limit=100):
        def g(k):
            return str(b.get(k) or "").strip()

        conds = []
        for k in ("name", "surname", "address", "disease"):
            if g(k):
                conds.append((k, g(k)))
        out = []
        num = g("number")
        idc = g("id")
        sex = g("sex")
        nofp = g("nofp") == "1"
        try:
            a1 = float(g("age1")) if g("age1") else None
        except Exception:
            a1 = None
        try:
            a2 = float(g("age2")) if g("age2") else None
        except Exception:
            a2 = None

        if not conds and not num and not idc and not sex and a1 is None and a2 is None and not nofp:
            return self._json({"ok": False, "err": "ใส่เงื่อนไขอย่างน้อย 1 อย่าง"})

        for p in PAT:
            ok = True
            for k, v in conds:
                if v not in (p.get(k) or ""):
                    ok = False
                    break
            if ok and num and str(p.get("number") or "") != num:
                ok = False
            if ok and idc and idc not in (p.get("id_card") or ""):
                ok = False
            if ok and sex and (p.get("sex") or "") != sex:
                ok = False
            if ok and (a1 is not None or a2 is not None):
                try:
                    ag = float(p.get("age") or 0)
                except Exception:
                    ag = 0
                if a1 is not None and ag < a1:
                    ok = False
                if a2 is not None and ag > a2:
                    ok = False
            if ok and nofp and (p.get("hn_uid") or "") in FP:
                ok = False
            if ok:
                q = dict(p)
                _u = p.get("hn_uid") or ""
                _r = FP.get(_u) or {}
                q["hasfp"] = bool(_r)
                q["fingers"] = sorted((_r.get("f") or {}).keys())
                q["sids"] = {k: d.get("sid", 0) for k, d in (_r.get("f") or {}).items()}
                out.append(q)
                if len(out) > limit:
                    break

        capped = len(out) > limit
        audit(ip, dev, "search", f"พบ {len(out)}")
        return self._json({"ok": True, "rows": out[:limit], "capped": capped})


# ============================================================ เริ่มทำงาน
def check_env():
    problems = []
    if not os.path.exists(os.path.join(HERE, ".env")):
        problems.append("ไม่พบไฟล์ .env (ดูตัวอย่างใน .env.example)")
    else:
        if not SECRET:
            problems.append("ยังไม่ได้ตั้ง SECRET ใน .env")
        if not WORKER:
            problems.append("ยังไม่ได้ตั้ง WORKER ใน .env (ใช้ตรวจ PIN)")
        if not CF_SECRET:
            problems.append("ยังไม่ได้ตั้ง CF_SECRET ใน .env (= UPLOAD_SECRET ของ Cloudflare)")
        if not ADMIN_PIN:
            problems.append("ยังไม่ได้ตั้ง ADMIN_PIN ใน .env (PIN สำรองตอนเน็ตล่ม)")
    if not os.path.exists(DBF_PATH):
        problems.append(f"ไม่พบไฟล์ทะเบียน: {DBF_PATH}")
    return problems


def main():
    print("=" * 62)
    print("  เซิร์ฟเวอร์ทะเบียนคนไข้ + ลายนิ้วมือ  (v16)")
    print("  คลินิกปัตตานีการแพทย์")
    print("=" * 62)

    problems = check_env()
    if problems:
        print()
        for p in problems:
            print("  [!] " + p)
        print()

    load_dbf(force=True)
    load_fingers()
    load_queue()
    load_slots()
    n, sz = img_count_size()
    if n:
        log(f"[OK] ภาพลายนิ้วมือ {n:,} ไฟล์ ({sz/1024/1024:.1f} MB)")

    if PAT and not PAT[0].get("hn_uid"):
        print()
        print("  " + "!" * 56)
        print("  ยังไม่มีคอลัมน์ HN_UID ใน pat4.dbf")
        print("  ให้รัน add_hnuid.prg ที่ FoxPro ก่อน ไม่งั้นเก็บลายนิ้วมือไม่ได้")
        print("  " + "!" * 56)
    elif not PAT:
        print()
        print("  " + "!" * 56)
        print("  อ่านทะเบียนคนไข้ไม่ได้ (คนไข้ 0 คน)")
        print("  ปิดโปรแกรม FoxPro แล้วรอสักครู่ ระบบจะลองอ่านใหม่เอง")
        print("  " + "!" * 56)

    ip = my_ip()
    print()
    print("=" * 62)
    print("  พิมพ์ที่อยู่นี้ในมือถือ (เบราว์เซอร์ หรือในแอป):")
    print()
    print(f"        {ip}:{PORT}")
    print()
    print("=" * 62)
    print("  PC ไม่ต้องมีเครื่องสแกน - มือถือเป็นคนเทียบลายนิ้วมือ")
    print("  ปิดโปรแกรมด้วย Ctrl+C")
    print()

    threading.Thread(target=watcher, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nปิดเซิร์ฟเวอร์")


if __name__ == "__main__":
    main()
