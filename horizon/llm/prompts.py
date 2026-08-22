"""Prompt templates. Content is Thai (with mixed Thai/English input expected)."""

from datetime import datetime

from ..config import BANGKOK, CATEGORIES

_CATEGORY_LIST = " | ".join(CATEGORIES)

EXTRACTION_SYSTEM = f"""คุณคือระบบสกัดข้อมูลเหตุการณ์จากข่าว ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่นนอก JSON

สกัดข้อมูลจากข่าวที่ได้รับ และตอบตามโครงสร้างนี้เท่านั้น:
{{
  "actors": ["string"],
  "action": "string",
  "location": "string|null",
  "time": "ISO-8601|null",
  "categories": ["string"],
  "summary": "string",
  "confidence": 0.0
}}

กฎเคร่งครัด:
1. actors — ผู้กระทำหรือผู้เกี่ยวข้องหลัก (คน องค์กร ประเทศ) ทำชื่อให้เป็นมาตรฐาน:
   ถ้าเป็นชื่อที่มีทั้งภาษาอังกฤษและไทย ให้เก็บทั้งสองในสตริงเดียว
   เช่น "Fed (เฟด)", "ธนาคารแห่งประเทศไทย (BOT)", "Donald Trump (โดนัลด์ ทรัมป์)"
   ถ้าไม่มีผู้กระทำที่ชัดเจน ให้ตอบ []
2. action — สิ่งที่เกิดขึ้น เป็นวลีสั้น ๆ ภาษาไทย ถ้าระบุไม่ได้ให้ตอบ null
3. location — สถานที่ที่เหตุการณ์เกิด ถ้าข่าวไม่ระบุให้ตอบ null ห้ามเดา
4. time — เวลาที่เหตุการณ์เกิด รูปแบบ ISO-8601 (เช่น "2026-08-22" หรือ "2026-08-22T14:30:00+07:00")
   ผู้ใช้จะแจ้ง "วันที่เผยแพร่ข่าว" มาให้ ใช้ค่านี้เป็นจุดอ้างอิงเวลาเท่านั้น:
   - "วันนี้" / "เมื่อเช้า" → วันที่เผยแพร่
   - "เมื่อวาน" → วันที่เผยแพร่ลบ 1 วัน
   - วันที่ที่ไม่มีปีกำกับ เช่น "21 ส.ค." → ใช้ปีจากวันที่เผยแพร่ **ห้ามเดาปีเอง**
   **ถ้าข่าวระบุแค่ปี (เช่น "เมื่อปี 2024") หรือแค่เดือน ให้ตอบ null**
   ห้ามเติมเดือนหรือวันที่ที่ข่าวไม่ได้บอก เช่น ห้ามแปลง "ปี 2024" เป็น "2024-01-01" เด็ดขาด
   **ถ้าข่าวไม่ได้พูดถึงเวลาที่เหตุการณ์เกิดเลย ให้ตอบ null — ห้ามใส่วันที่เผยแพร่แทน**
5. categories — เลือกจากรายการปิดนี้เท่านั้น: {_CATEGORY_LIST}
   เลือก 1–3 รายการ เรียงจากตรงที่สุดไปน้อยที่สุด ห้ามสร้างหมวดใหม่
6. summary — สรุปภาษาไทย ไม่เกิน 280 ตัวอักษร ระบุตัวเลขและชื่อเฉพาะที่สำคัญไว้ด้วย
7. confidence — ความมั่นใจในการสกัดโดยรวม 0.0–1.0

ตอบ JSON เดียวเท่านั้น"""

EXTRACTION_USER = """วันที่เผยแพร่ข่าว: {published}

หัวข้อข่าว: {title}

เนื้อหาข่าว:
{body}"""

REPAIR_SYSTEM = """ข้อความต่อไปนี้ควรเป็น JSON ตามโครงสร้างที่กำหนด แต่ผิดรูปแบบ
แก้ไขให้เป็น JSON ที่ถูกต้องตามโครงสร้างเดิม โดยไม่เพิ่มหรือแต่งข้อมูลใหม่
ถ้าฟิลด์ใดหาไม่ได้ ให้ใส่ null (หรือ [] สำหรับ array)
ตอบเป็น JSON เดียวเท่านั้น ห้ามมีข้อความอื่น"""

REPAIR_USER = """โครงสร้างที่ต้องการ:
{{
  "actors": ["string"],
  "action": "string|null",
  "location": "string|null",
  "time": "ISO-8601|null",
  "categories": ["string"],
  "summary": "string",
  "confidence": 0.0
}}

ข้อความที่ต้องแก้:
{broken}"""


def extraction_messages(
    title: str,
    body: str,
    *,
    published_at: datetime | None = None,
    body_limit: int = 6000,
) -> list[dict[str, str]]:
    """Build the extraction prompt.

    `published_at` is the anchor for relative dates. Thai news routinely writes
    "21 ส.ค." with no year and "วันนี้" with no date; without an anchor the model
    invents one, which then poisons the temporal feature used for clustering.
    """
    published = (
        published_at.astimezone(BANGKOK).strftime("%Y-%m-%d")
        if published_at
        else "ไม่ทราบ"
    )
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {
            "role": "user",
            "content": EXTRACTION_USER.format(
                published=published,
                title=title or "(ไม่มีหัวข้อ)",
                body=(body or "")[:body_limit],
            ),
        },
    ]


def repair_messages(broken: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": REPAIR_SYSTEM},
        {"role": "user", "content": REPAIR_USER.format(broken=broken[:4000])},
    ]
