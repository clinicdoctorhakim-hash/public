*=========================================================
* fill_uid.prg  -  เติม HN_UID ให้ "แถวปัจจุบัน" ถ้ายังว่าง
* คลินิกปัตตานีการแพทย์
*
* ใช้ตอนไหน:
*   เรียกทันทีหลังบันทึกคนไข้ใหม่ (หลัง APPEND BLANK + REPLACE เสร็จ)
*   เช่น    APPEND BLANK
*           REPLACE NUMBER WITH ..., NAME WITH ...
*           DO fill_uid          && <-- ใส่บรรทัดนี้
*
* ปลอดภัย:
*   - ทำงานกับตารางที่ FoxPro เปิดอยู่แล้ว ไม่ต้องปิดโปรแกรม
*   - ถ้ายังไม่มีคอลัมน์ HN_UID จะไม่ทำอะไร (ไม่ error)
*   - ถ้าแถวนี้มีค่าอยู่แล้ว จะไม่เขียนทับ
*   - เร็วมาก (ทำแค่แถวเดียว)
*
* รูปแบบค่า:  <เลขคนไข้>_F<6 ตัว เวลา><4 ตัว ลำดับแถว>
*   ตัว F = สร้างจาก FoxPro  (ระบบมือถือ/PC ใช้ P กันชนกัน)
*=========================================================

PRIVATE lcRun, lcNum

*--- ไม่มีคอลัมน์ HN_UID -> ออกเงียบๆ ---
IF TYPE("HN_UID") = "U"
   RETURN
ENDIF

*--- มีค่าอยู่แล้ว -> ไม่แตะ ---
IF NOT EMPTY(HN_UID)
   RETURN
ENDIF

*--- เวลาเป็นฐาน 36 (6 ตัว) + ลำดับแถวเป็นฐาน 36 (4 ตัว) ---
lcRun = b36u((DATE() - {^2020-01-01}) * 86400 + ;
             VAL(SUBSTR(TIME(), 1, 2)) * 3600 + ;
             VAL(SUBSTR(TIME(), 4, 2)) * 60 + ;
             VAL(SUBSTR(TIME(), 7, 2)), 6)

lcNum = ALLTRIM(STR(NUMBER))
IF EMPTY(lcNum)
   lcNum = "00000"
ENDIF

REPLACE HN_UID WITH lcNum + "_F" + lcRun + b36u(RECNO(), 4)

RETURN


*=========================================================
* แปลงเลขเป็นฐาน 36 (0-9, A-Z) เติม 0 ข้างหน้าให้ครบความกว้าง
* ชื่อ b36u กันชนกับ b36 ใน add_hnuid.prg
*=========================================================
FUNCTION b36u
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
