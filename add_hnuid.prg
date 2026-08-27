*=========================================================
* add_hnuid.prg  -  เพิ่มคอลัมน์ HN_UID ลง pat4.dbf
* คลินิกปัตตานีการแพทย์
*
* ทำอะไร (ตามลำดับ):
*   1. สำรอง pat4.dbf + pat4.fpt ไว้ก่อน
*   2. เพิ่มคอลัมน์ HN_UID C(24) ถ้ายังไม่มี
*   3. เติมค่าให้ทุกแถวที่ยังว่าง
*   4. ตรวจว่าซ้ำกันไหม แล้วรายงานผล
*
* รูปแบบค่า:  <เลขคนไข้>_F<6 ตัว เวลา><4 ตัว ลำดับ>
*   ตัวอย่าง:  32258_F3GEZDH0001
*   ตัว F = สร้างจาก FoxPro   (ระบบมือถือ/PC ใช้ P กันชนกัน)
*
* วิธีใช้:
*   - ปิดโปรแกรมหลัก (c5) ให้หมดก่อน
*   - ห้ามมีใครเปิด pat4 ค้างไว้
*   - แล้วสั่ง:  DO add_hnuid.prg
*=========================================================

SET TALK OFF
SET SAFETY OFF
SET EXCLUSIVE ON
SET DELETED OFF
CLEAR

PRIVATE lcDir, lcDbf, lcFpt, lcBak, lcRun, lnTotal, lnFill, lnSkip, lnDup, lcStamp

*--- ที่อยู่ไฟล์ (แก้ตรงนี้ถ้าย้ายที่) ---
lcDir = "D:\Zipdrive\foxpro\"
lcDbf = lcDir + "pat4.dbf"
lcFpt = lcDir + "pat4.fpt"

? "=============================================="
? "  เพิ่มคอลัมน์ HN_UID ลง pat4.dbf"
? "=============================================="
?

IF NOT FILE(lcDbf)
   ? "[X] ไม่พบไฟล์: " + lcDbf
   ? "    แก้บรรทัด lcDir ในโปรแกรมนี้ให้ถูกต้อง"
   RETURN
ENDIF

*--- 1) สำรองไฟล์ ---------------------------------------
lcStamp = DTOS(DATE()) + STRTRAN(SUBSTR(TIME(),1,5), ":", "")
lcBak = lcDir + "pat4_backup\"
IF NOT DIRECTORY(lcBak)
   MD (lcBak)
ENDIF

? "1) สำรองไฟล์..."
COPY FILE (lcDbf) TO (lcBak + "pat4_" + lcStamp + "_ก่อนเพิ่มUID.dbf")
IF FILE(lcFpt)
   COPY FILE (lcFpt) TO (lcBak + "pat4_" + lcStamp + "_ก่อนเพิ่มUID.fpt")
ENDIF
? "   เก็บไว้ที่ " + lcBak
?

*--- 2) เปิดแบบผูกขาด -----------------------------------
? "2) เปิดตาราง..."
ON ERROR DO errHandle WITH ERROR(), MESSAGE()
USE (lcDbf) EXCLUSIVE ALIAS pat
ON ERROR

IF NOT USED("pat")
   ? "[X] เปิดตารางไม่ได้ - อาจมีคนเปิดค้างอยู่"
   ? "    ปิดโปรแกรมหลักให้หมดแล้วลองใหม่"
   RETURN
ENDIF

lnTotal = RECCOUNT()
? "   มีทั้งหมด " + TRANSFORM(lnTotal, "999,999") + " แถว"
?

*--- 3) เพิ่มคอลัมน์ ------------------------------------
? "3) ตรวจคอลัมน์ HN_UID..."
IF TYPE("pat.HN_UID") = "U"
   ? "   ยังไม่มี - กำลังเพิ่ม C(24) ..."
   ALTER TABLE pat ADD COLUMN HN_UID C(24)
   IF TYPE("pat.HN_UID") = "U"
      ? "[X] เพิ่มคอลัมน์ไม่สำเร็จ"
      USE
      RETURN
   ENDIF
   ? "   เพิ่มแล้ว"
ELSE
   ? "   มีอยู่แล้ว - ข้ามขั้นนี้"
ENDIF
?

*--- 4) เติมค่าให้แถวที่ยังว่าง -------------------------
* runstamp = วินาทีตั้งแต่ 1 ม.ค. 2020 แปลงเป็นฐาน 36 (6 ตัว)
lcRun = b36((DATE() - {^2020-01-01}) * 86400 + ;
            VAL(SUBSTR(TIME(),1,2))*3600 + ;
            VAL(SUBSTR(TIME(),4,2))*60 + ;
            VAL(SUBSTR(TIME(),7,2)), 6)

? "4) เติมค่า (รหัสรอบนี้: " + lcRun + ") ..."
lnFill = 0
lnSkip = 0

GO TOP
SCAN
   IF EMPTY(pat.HN_UID)
      REPLACE pat.HN_UID WITH ;
         ALLTRIM(STR(pat.NUMBER)) + "_F" + lcRun + b36(RECNO(), 4)
      lnFill = lnFill + 1
      IF MOD(lnFill, 5000) = 0
         ?? "."
      ENDIF
   ELSE
      lnSkip = lnSkip + 1
   ENDIF
ENDSCAN
?
? "   เติมใหม่ " + TRANSFORM(lnFill, "999,999") + " แถว"
? "   มีอยู่แล้ว " + TRANSFORM(lnSkip, "999,999") + " แถว"
?

*--- 5) ตรวจว่าซ้ำกันไหม --------------------------------
? "5) ตรวจค่าซ้ำ..."
INDEX ON HN_UID TAG chkuid
GO TOP
lnDup = 0
lcLast = ""
SCAN
   IF NOT EMPTY(pat.HN_UID) AND ALLTRIM(pat.HN_UID) == lcLast
      lnDup = lnDup + 1
      IF lnDup <= 5
         ? "   [!] ซ้ำ: " + ALLTRIM(pat.HN_UID) + "  แถว " + TRANSFORM(RECNO())
      ENDIF
   ENDIF
   lcLast = ALLTRIM(pat.HN_UID)
ENDSCAN
DELETE TAG chkuid

IF lnDup = 0
   ? "   ไม่มีค่าซ้ำ"
ELSE
   ? "   [!] พบซ้ำ " + TRANSFORM(lnDup) + " รายการ - แจ้งผู้ดูแลระบบ"
ENDIF
?

*--- 6) รายงานสรุป --------------------------------------
GO TOP
? "=============================================="
? "  สรุป"
? "=============================================="
? "  แถวทั้งหมด    : " + TRANSFORM(RECCOUNT(), "999,999")
? "  เติมใหม่รอบนี้ : " + TRANSFORM(lnFill, "999,999")
? "  มีอยู่ก่อนแล้ว : " + TRANSFORM(lnSkip, "999,999")
? "  ค่าซ้ำ         : " + TRANSFORM(lnDup)
?
? "  ตัวอย่าง 3 แถวแรก:"
GO TOP
FOR i = 1 TO 3
   IF NOT EOF()
      ? "    " + PADR(ALLTRIM(pat.NAME), 22) + " -> " + ALLTRIM(pat.HN_UID)
      SKIP
   ENDIF
ENDFOR
?
? "  ไฟล์สำรองอยู่ที่ " + lcBak
? "  ถ้าผิดพลาด ก๊อปไฟล์สำรองกลับมาทับได้"
?

USE
? "เสร็จเรียบร้อย - กดปุ่มใดๆ เพื่อจบ"
WAIT WINDOW "" TIMEOUT 3
RETURN


*=========================================================
* แปลงเลขเป็นฐาน 36 (0-9, A-Z) เติม 0 ข้างหน้าให้ครบความกว้าง
*=========================================================
FUNCTION b36
   LPARAMETERS tnNum, tnWidth
   LOCAL lcD, lcOut, lnN
   lcD = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
   lcOut = ""
   lnN = INT(tnNum)
   DO WHILE lnN > 0
      lcOut = SUBSTR(lcD, MOD(lnN, 36) + 1, 1) + lcOut
      lnN = INT(lnN / 36)
   ENDDO
   DO WHILE LEN(lcOut) < tnWidth
      lcOut = "0" + lcOut
   ENDDO
   RETURN RIGHT(lcOut, tnWidth)
ENDFUNC


*=========================================================
* ดักข้อผิดพลาดตอนเปิดตาราง
*=========================================================
PROCEDURE errHandle
   LPARAMETERS tnErr, tcMsg
   ? "[X] ผิดพลาด " + TRANSFORM(tnErr) + ": " + tcMsg
   RETURN
ENDPROC
