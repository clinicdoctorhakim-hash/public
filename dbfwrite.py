# -*- coding: utf-8 -*-
"""
dbfwrite.py - เขียนข้อมูลลง pat4.dbf อย่างปลอดภัย
คลินิกปัตตานีการแพทย์

หลักความปลอดภัย:
  1. สำรอง .dbf + .fpt ทุกครั้งก่อนเขียน
  2. ตรวจว่า FoxPro ปิดไฟล์แล้ว (ลองเปิดแบบเขียน)
  3. เขียนเสร็จแล้วอ่านกลับมาตรวจ ถ้าไม่ตรงให้กู้คืนทันที
"""

import os
import shutil
import struct
import time

ENC = "cp874"


# ---------------------------------------------------------------- อ่านโครงสร้าง
def read_schema(dbf_path):
    """อ่านโครงสร้างคอลัมน์จากไฟล์จริง (ไม่ใช้ค่าที่พิมพ์ค้างไว้)"""
    with open(dbf_path, "rb") as f:
        head = f.read(32)
        nrec = struct.unpack("<I", head[4:8])[0]
        hlen = struct.unpack("<H", head[8:10])[0]
        rlen = struct.unpack("<H", head[10:12])[0]
        f.seek(0)
        hdr = f.read(hlen)
    fields = []
    off = 1                      # ไบต์แรกของแถวคือธงลบ
    i = 32
    while i < hlen - 1 and hdr[i] != 0x0D:
        name = hdr[i:i + 11].split(b"\x00")[0].decode("ascii", "replace")
        ftype = chr(hdr[i + 11])
        width = hdr[i + 16]
        dec = hdr[i + 17]
        fields.append({"name": name, "type": ftype, "off": off, "w": width, "dec": dec})
        off += width
        i += 32
    return {"nrec": nrec, "hlen": hlen, "rlen": rlen, "fields": fields}


def field_map(schema):
    return {f["name"]: f for f in schema["fields"]}


# ---------------------------------------------------------------- ตรวจ/สำรอง
def is_locked(path):
    """FoxPro เปิดไฟล์ค้างอยู่ไหม (ลองเปิดแบบเขียน)"""
    try:
        with open(path, "r+b"):
            return False
    except Exception:
        return True


def backup(dbf_path, keep=20):
    """สำรอง .dbf + .fpt พร้อมเวลา คืน path ที่สำรองไว้"""
    base = os.path.splitext(dbf_path)[0]
    d = os.path.join(os.path.dirname(dbf_path), "pat4_backup")
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = []
    for ext in (".DBF", ".FPT"):
        src = base + ext
        if not os.path.exists(src):
            src = base + ext.lower()
        if os.path.exists(src):
            dst = os.path.join(d, f"pat4_{stamp}{ext.lower()}")
            shutil.copy2(src, dst)
            out.append(dst)
    # เก็บย้อนหลังไม่เกิน keep ชุด
    try:
        files = sorted(os.listdir(d))
        dbfs = [x for x in files if x.endswith(".dbf")]
        while len(dbfs) > keep:
            old = dbfs.pop(0)
            for ext in (".dbf", ".fpt"):
                p = os.path.join(d, old[:-4] + ext)
                if os.path.exists(p):
                    os.remove(p)
    except Exception:
        pass
    return out


def restore(dbf_path, backups):
    """กู้คืนจากไฟล์สำรอง"""
    base = os.path.splitext(dbf_path)[0]
    for b in backups:
        ext = os.path.splitext(b)[1]
        shutil.copy2(b, base + ext.upper() if os.path.exists(base + ext.upper()) else base + ext)


# ---------------------------------------------------------------- memo (.fpt)
def fpt_path(dbf_path):
    base = os.path.splitext(dbf_path)[0]
    for ext in (".FPT", ".fpt"):
        if os.path.exists(base + ext):
            return base + ext
    return None


def memo_append(fpt, text):
    """เขียนข้อความลงท้าย .fpt คืนเลขบล็อก (0 = ข้อความว่าง)"""
    if text is None or str(text).strip() == "":
        return 0
    data = str(text).encode(ENC, "replace")
    with open(fpt, "r+b") as f:
        head = f.read(8)
        nxt = struct.unpack(">I", head[0:4])[0]
        bs = struct.unpack(">H", head[6:8])[0]
        if bs <= 0:
            bs = 64
        f.seek(0, 2)
        end = f.tell()
        # เขียนต่อท้ายไฟล์จริง (กันกรณีตัวเลขในหัวไฟล์ไม่ตรงกับขนาดจริง)
        start_block = (end + bs - 1) // bs
        f.seek(start_block * bs)
        blk = struct.pack(">I", 1) + struct.pack(">I", len(data)) + data
        pad = (-len(blk)) % bs
        f.write(blk + b"\x00" * pad)
        newend = f.tell()
        f.seek(0)
        f.write(struct.pack(">I", newend // bs))
    return start_block


def memo_read(fpt, block):
    if not block:
        return ""
    try:
        with open(fpt, "rb") as f:
            head = f.read(8)
            bs = struct.unpack(">H", head[6:8])[0] or 64
            f.seek(block * bs)
            h = f.read(8)
            ln = struct.unpack(">I", h[4:8])[0]
            return f.read(ln).decode(ENC, "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------- HN_UID
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _b36(n, w):
    out = ""
    n = int(n)
    while n > 0:
        out = _B36[n % 36] + out
        n //= 36
    return out.rjust(w, "0")[-w:]


def make_hn_uid(number, seq, runstamp=None):
    """
    สร้าง HN_UID: "<เลขคนไข้>_P<6 ตัว เวลา><4 ตัว ลำดับ>"
    ตัว P = สร้างจากระบบนี้ (FoxPro ใช้ F) กันชนกันสนิท
    """
    if runstamp is None:
        runstamp = _b36(int(time.time()) - 1577836800, 6)
    hn = str(number if number is not None else "").strip() or "00000"
    return f"{hn}_P{runstamp}{_b36(seq, 4)}"


# ---------------------------------------------------------------- แปลงค่า
def pack_value(fld, val):
    """แปลงค่าให้เป็นไบต์ตามชนิดและความกว้างของคอลัมน์"""
    w = fld["w"]
    t = fld["type"]
    if t == "C":
        b = str(val if val is not None else "").encode(ENC, "replace")[:w]
        return b + b" " * (w - len(b))
    if t == "N":
        if val is None or str(val).strip() == "":
            return b" " * w
        try:
            if fld["dec"] > 0:
                s = f"{float(val):.{fld['dec']}f}"
            else:
                s = str(int(float(val)))
        except Exception:
            return b" " * w
        s = s[:w]
        return s.rjust(w).encode("ascii", "replace")
    if t == "D":
        s = str(val or "").replace("-", "").replace("/", "")[:8]
        return s.rjust(8).encode("ascii") if s else b" " * 8
    if t == "L":
        v = str(val).upper()
        return b"T" if v in ("T", "TRUE", "Y", "1") else (b"F" if v in ("F", "FALSE", "N", "0") else b" ")
    return b" " * w


# ---------------------------------------------------------------- เขียน
def apply_queue(dbf_path, items, log=print):
    """
    เขียนรายการในคิวลง .dbf
      items: [{"kind":"add"/"edit", "rid":int(เฉพาะ edit), "data":{...}}]
    คืน {"ok":bool, "added":n, "edited":n, "backup":[...], "err":str, "newrids":{คิวที่->rid}}
    """
    if not os.path.exists(dbf_path):
        return {"ok": False, "err": "ไม่พบไฟล์ " + dbf_path}
    if is_locked(dbf_path):
        return {"ok": False, "err": "ไฟล์ถูกใช้งานอยู่ - ปิดโปรแกรม FoxPro ก่อน"}

    fpt = fpt_path(dbf_path)
    if fpt and is_locked(fpt):
        return {"ok": False, "err": "ไฟล์ memo (.fpt) ถูกใช้งานอยู่ - ปิด FoxPro ก่อน"}

    schema = read_schema(dbf_path)
    fm = field_map(schema)
    bks = backup(dbf_path)
    log(f"สำรองไฟล์แล้ว: {len(bks)} ไฟล์")

    memo_names = [f["name"] for f in schema["fields"] if f["type"] == "M"]
    added = 0
    edited = 0
    newrids = {}
    newuids = {}
    uid_seq = 0
    runstamp = _b36(int(time.time()) - 1577836800, 6)
    if "HN_UID" not in fm:
        log("[!] ยังไม่มีคอลัมน์ HN_UID - แถวใหม่จะเก็บลายนิ้วมือไม่ได้ (รัน add_hnuid.prg)")

    try:
        with open(dbf_path, "r+b") as f:
            nrec = schema["nrec"]
            hlen = schema["hlen"]
            rlen = schema["rlen"]

            for idx, it in enumerate(items):
                kind = it.get("kind")
                data = it.get("data") or {}

                if kind == "edit":
                    rid = int(it.get("rid") or 0)
                    if rid < 1 or rid > nrec:
                        log(f"  ข้าม: ลำดับ {rid} ไม่ถูกต้อง")
                        continue
                    pos = hlen + (rid - 1) * rlen
                    f.seek(pos)
                    rec = bytearray(f.read(rlen))
                    for k, v in data.items():
                        fld = fm.get(k.upper())
                        if not fld:
                            continue
                        if fld["type"] == "M":
                            blk = memo_append(fpt, v) if fpt else 0
                            rec[fld["off"]:fld["off"] + 4] = struct.pack("<I", blk)
                        else:
                            rec[fld["off"]:fld["off"] + fld["w"]] = pack_value(fld, v)
                    f.seek(pos)
                    f.write(bytes(rec))
                    edited += 1

                elif kind == "add":
                    # ถ้ามีคอลัมน์ HN_UID และยังไม่ได้ส่งค่ามา -> สร้างให้อัตโนมัติ
                    if "HN_UID" in fm and not str(data.get("HN_UID") or "").strip():
                        data = dict(data)
                        uid_seq += 1
                        data["HN_UID"] = make_hn_uid(data.get("NUMBER"), uid_seq, runstamp)
                        newuids[str(idx)] = data["HN_UID"]
                    rec = bytearray(b" " * rlen)
                    rec[0] = 0x20                      # ไม่ถูกลบ
                    for fld in schema["fields"]:
                        if fld["type"] == "M":
                            rec[fld["off"]:fld["off"] + 4] = struct.pack("<I", 0)
                    for k, v in data.items():
                        fld = fm.get(k.upper())
                        if not fld:
                            continue
                        if fld["type"] == "M":
                            blk = memo_append(fpt, v) if fpt else 0
                            rec[fld["off"]:fld["off"] + 4] = struct.pack("<I", blk)
                        else:
                            rec[fld["off"]:fld["off"] + fld["w"]] = pack_value(fld, v)
                    # เขียนต่อท้ายแถวสุดท้าย
                    f.seek(hlen + nrec * rlen)
                    f.write(bytes(rec))
                    nrec += 1
                    newrids[str(idx)] = nrec       # ลำดับแถวใหม่ = จำนวนแถวหลังเพิ่ม
                    added += 1

            # ปิดท้ายไฟล์ + อัปเดตจำนวนแถวในหัวไฟล์
            f.seek(hlen + nrec * rlen)
            f.write(b"\x1A")
            f.truncate()
            f.seek(4)
            f.write(struct.pack("<I", nrec))
            t = time.localtime()
            f.seek(1)
            f.write(bytes([t.tm_year % 100, t.tm_mon, t.tm_mday]))
    except Exception as e:
        log(f"[X] เขียนไม่สำเร็จ: {e} - กำลังกู้คืน")
        try:
            restore(dbf_path, bks)
            log("กู้คืนจากไฟล์สำรองแล้ว")
        except Exception as e2:
            log(f"[X] กู้คืนไม่สำเร็จ: {e2}")
        return {"ok": False, "err": str(e)[:200], "backup": bks}

    # ตรวจสอบว่าอ่านกลับได้จริง
    try:
        s2 = read_schema(dbf_path)
        if s2["nrec"] != schema["nrec"] + added:
            raise ValueError(f"จำนวนแถวไม่ตรง ({s2['nrec']} ควรเป็น {schema['nrec'] + added})")
    except Exception as e:
        log(f"[X] ตรวจหลังเขียนไม่ผ่าน: {e} - กู้คืน")
        restore(dbf_path, bks)
        return {"ok": False, "err": "ตรวจหลังเขียนไม่ผ่าน: " + str(e)[:150], "backup": bks}

    log(f"[OK] เพิ่ม {added} แถว · แก้ {edited} แถว")
    return {"ok": True, "added": added, "edited": edited, "backup": bks,
            "newrids": newrids, "newuids": newuids}
