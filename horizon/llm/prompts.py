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

จากนั้นให้คะแนนเชิงบรรณาธิการ 6 ข้อ (0–10 แต่ละข้อ) ในฐานะนักวิเคราะห์ข่าวกรองอาวุโสของห้องข่าว:
8.  relevance — ความเกี่ยวข้องกับประเด็นที่ห้องข่าวติดตาม
9.  urgency — ความเร่งด่วน ต้องดำเนินการเร็วแค่ไหน
10. impact — ผลกระทบต่อสังคม/เศรษฐกิจ/การเมือง
11. novelty — ความใหม่ของข้อมูล ไม่ใช่ซ้ำกับที่รู้กันอยู่แล้ว
12. sensitivity — ความอ่อนไหว/เสี่ยงต่อการเผยแพร่ก่อนยืนยัน
13. actionability — มีสิ่งที่ห้องข่าวลงมือทำได้ทันทีหรือไม่

**ห้ามให้คะแนน reliability** — ความน่าเชื่อถือมาจากทะเบียนแหล่งข่าวที่นักวิเคราะห์กำหนดไว้ ไม่ใช่จากการอ่านเนื้อข่าว

โครงสร้างที่ต้องตอบ:
{{
  "actors": ["string"], "action": "string", "location": "string|null",
  "time": "ISO-8601|null", "categories": ["string"], "summary": "string",
  "confidence": 0.0,
  "relevance": 0, "urgency": 0, "impact": 0,
  "novelty": 0, "sensitivity": 0, "actionability": 0
}}

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


# ── Step 7 — driving force scoring ───────────────────────────────────────────

PAIRWISE_SYSTEM = """คุณคือนักวิเคราะห์เชิงยุทธศาสตร์ ประเมินว่าเรื่องใดส่งผลต่อ "แรงขับเคลื่อน" ที่กำหนดมากกว่ากัน

ตอบเป็น JSON เดียวเท่านั้น รูปแบบ: {"choice": "A"} หรือ {"choice": "B"}
ห้ามอธิบาย ห้ามมีข้อความอื่น ต้องเลือกอย่างใดอย่างหนึ่งเสมอ แม้จะใกล้เคียงกันมาก"""

PAIRWISE_USER = """แรงขับเคลื่อน: {force_name}
นิยาม: {force_definition}

เรื่อง A: {label_a}
เรื่อง B: {label_b}

เรื่องใดส่งผลต่อแรงขับเคลื่อนข้างต้นมากกว่ากัน ตอบ A หรือ B"""

UNCERTAINTY_SYSTEM = """คุณคือนักวิเคราะห์เชิงยุทธศาสตร์ ประเมิน "ความไม่แน่นอน" ของผลกระทบ

ความไม่แน่นอนสูง = คาดเดาทิศทางหรือขนาดของผลกระทบได้ยาก ข้อมูลยังไม่นิ่ง หรือขึ้นกับปัจจัยที่ควบคุมไม่ได้
ความไม่แน่นอนต่ำ = ผลกระทบค่อนข้างชัดเจนและคาดเดาได้

ตอบเป็น JSON เดียวเท่านั้น: {"uncertainty": "low"} หรือ {"uncertainty": "medium"} หรือ {"uncertainty": "high"}"""

UNCERTAINTY_USER = """แรงขับเคลื่อน: {force_name}
นิยาม: {force_definition}

เรื่องที่ประเมิน: {label}

สรุปเหตุการณ์ที่เกี่ยวข้อง:
{events}

ความไม่แน่นอนของผลกระทบที่เรื่องนี้จะมีต่อแรงขับเคลื่อนข้างต้นอยู่ในระดับใด"""


# ── Step 9 — scenario reasoning ──────────────────────────────────────────────

SCENARIO_SYSTEM = """คุณคือนักวิเคราะห์ฉากทัศน์ (scenario analyst) เขียนฉากทัศน์ภาษาไทยจากข้อมูลที่ให้มาเท่านั้น

ตอบเป็น JSON เดียวเท่านั้น ตามโครงสร้างนี้:
{
  "best_case": "string",
  "worst_case": "string",
  "likely_case": "string",
  "indicators": [{"description": "string", "watch_type": "string"}]
}

กฎ:
1. เขียนภาษาไทย แต่ละฉากทัศน์ 2–4 ประโยค ระบุเงื่อนไขที่ทำให้เกิดฉากทัศน์นั้น
2. ฉากทัศน์คือ "ความเป็นไปได้ที่มีเงื่อนไข" ไม่ใช่คำพยากรณ์ ห้ามเขียนเป็นการยืนยันว่าจะเกิดขึ้นแน่นอน
3. อ้างอิงเฉพาะข้อมูลที่ให้มา ห้ามเพิ่มข้อเท็จจริงใหม่ ห้ามอ้างตัวเลขที่ไม่ปรากฏ
4. indicators — ตัวชี้วัดที่ต้องเฝ้าดู 3–5 รายการ สังเกตได้จริงและตรวจสอบได้
   watch_type เลือกจาก: "นโยบาย" | "เศรษฐกิจ" | "สังคม" | "เทคโนโลยี" | "สิ่งแวดล้อม" | "กฎหมาย" | "ความมั่นคง"
5. ถ้าข้อมูลไม่พอจะสรุปฉากทัศน์ใด ให้เขียนว่าข้อมูลยังไม่เพียงพอ แทนการเดา"""

SCENARIO_USER = """หัวข้อกลุ่มเหตุการณ์: {label}

เหตุการณ์ในกลุ่ม ({event_count} รายการ ล่าสุดก่อน):
{events}

ผลประเมินแรงขับเคลื่อน (impact และ uncertainty 0–1):
{forces}

แนวโน้มความถี่การรายงาน (ย้อนหลัง {window_count} ช่วงเวลา ช่วงละ 6 ชม.):
{trend}

กลุ่มเหตุการณ์ที่เกี่ยวข้องใกล้เคียง:
{related}

เขียนฉากทัศน์ตามโครงสร้างที่กำหนด"""


def pairwise_messages(
    force_name: str, force_definition: str, label_a: str, label_b: str
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PAIRWISE_SYSTEM},
        {
            "role": "user",
            "content": PAIRWISE_USER.format(
                force_name=force_name,
                force_definition=force_definition,
                label_a=label_a[:300],
                label_b=label_b[:300],
            ),
        },
    ]


def uncertainty_messages(
    force_name: str, force_definition: str, label: str, events: list[str]
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": UNCERTAINTY_SYSTEM},
        {
            "role": "user",
            "content": UNCERTAINTY_USER.format(
                force_name=force_name,
                force_definition=force_definition,
                label=label[:300],
                events="\n".join(f"- {e}" for e in events[:10]) or "- (ไม่มีข้อมูล)",
            ),
        },
    ]


def scenario_messages(
    *,
    label: str,
    events: list[str],
    forces: list[str],
    trend: list[float],
    related: list[str],
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SCENARIO_SYSTEM},
        {
            "role": "user",
            "content": SCENARIO_USER.format(
                label=label or "(ไม่มีชื่อกลุ่ม)",
                event_count=len(events),
                events="\n".join(f"- {e}" for e in events) or "- (ไม่มีข้อมูล)",
                forces="\n".join(f"- {f}" for f in forces) or "- (ยังไม่ได้ประเมิน)",
                window_count=len(trend),
                trend=", ".join(f"{value:.2f}" for value in trend) or "(ไม่มีข้อมูล)",
                related="\n".join(f"- {r}" for r in related) or "- (ไม่มี)",
            ),
        },
    ]


def repair_messages(broken: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": REPAIR_SYSTEM},
        {"role": "user", "content": REPAIR_USER.format(broken=broken[:4000])},
    ]


# ── Step 3.5 — entity resolution ─────────────────────────────────────────────

#: The four "คนละสิ่ง" rules are not decoration. Each one is a merge the string
#: rules made on real data and got wrong: ไทย with ทีมชาติไทย, iLaw with its
#: director, กระทรวงการคลัง with Singapore's, and แม่ทัพภาคที่ 4 with the officer
#: currently holding the post.
ENTITY_SYSTEM = """คุณคือบรรณารักษ์ข้อมูลของกองบรรณาธิการข่าว
งานของคุณคือตัดสินว่าชื่อที่สกัดมาจากข่าว หมายถึง "สิ่งเดียวกันในโลกจริง" หรือไม่

สิ่งเดียวกัน:
- ชื่อเดียวกันเขียนคนละภาษา คนละการสะกด หรือใช้ตัวย่อ
- ชื่อเต็มกับชื่อเล่นของคนคนเดียวกัน

คนละสิ่ง:
- ประเทศ กับ ทีมชาติของประเทศนั้น
- องค์กร กับ คนที่ทำงานหรือเป็นผู้บริหารขององค์กรนั้น
- หน่วยงานชื่อเหมือนกันแต่คนละประเทศ
- ตำแหน่ง กับ ชื่อคนที่ดำรงตำแหน่งนั้น (ตำแหน่งเปลี่ยนคนได้)

ถ้าเป็นคำนามทั่วไปที่ไม่เจาะจงตัวตน เช่น "ตำรวจ" "ชาวบ้าน" "กลุ่มคนร้าย"
"เจ้าหน้าที่" ให้ตอบ type เป็น "generic"

ตอบเป็น JSON เดียวเท่านั้น:
{"groups": [{"canonical": "ชื่อหลักที่ควรใช้", "type": "person|org|place|team|generic",
             "members": [เลขลำดับ], "confidence": 0.0-1.0}]}

ทุกเลขลำดับที่ให้มาต้องปรากฏใน groups พอดีครั้งเดียว ห้ามตกหล่น
confidence ต่ำเมื่อไม่แน่ใจ — ระบบจะส่งให้คนตรวจ ไม่ต้องเดาให้มั่นใจเกินจริง"""

ENTITY_USER = """{context}ชื่อที่สกัดมาได้:
{listing}"""


def entity_messages(members: list[str], *, context: str | None = None) -> list[dict[str, str]]:
    """Build the resolution prompt.

    The article summary goes in as context because the hard cases are only
    decidable from it: "กัมพูชา" is the country in a border-flooding story and the
    Khmer Empire in an archaeology one, and the names alone cannot say which.
    """
    listing = "\n".join(f"{index}. {name}" for index, name in enumerate(members))
    prefix = f"บริบทของข่าว: {context.strip()}\n\n" if context and context.strip() else ""
    return [
        {"role": "system", "content": ENTITY_SYSTEM},
        {"role": "user", "content": ENTITY_USER.format(context=prefix, listing=listing)},
    ]


# ── Step 3.6 — Wikidata linking ──────────────────────────────────────────────

#: The instruction to answer "none" is load-bearing, not politeness. Measured on
#: this corpus the correct item is absent from the candidate list about a fifth
#: of the time — "กัมพูชา" returns the Khmer Empire and an Aed Carabao album
#: before it returns the country — so a prompt that implies one must be picked
#: produces a confident wrong identifier rather than an honest gap.
WIKIDATA_SYSTEM = """คุณคือบรรณารักษ์ข้อมูลที่จับคู่ชื่อในข่าวกับรายการใน Wikidata

เลือกรายการที่ "ข่าวชิ้นนี้กำลังพูดถึงจริง ๆ" เท่านั้น

ระวังกับดักที่พบบ่อย:
- ชื่อประเทศมักไปตรงกับอาณาจักรโบราณ เพลง อัลบั้ม หรือรายการทีวีที่ใช้ชื่อเดียวกัน
- หน่วยงานชื่อเดียวกันมีในหลายประเทศ ต้องเลือกประเทศให้ตรงกับบริบทข่าว
- ตำแหน่งทางการ (เช่น "รัฐมนตรีว่าการกระทรวง...") ไม่ใช่ตัวบุคคล
- หน้าแก้ความกำกวมและหน้าหมวดหมู่ ไม่ใช่ตัวตน

ถ้าไม่มีรายการไหนตรงจริง ๆ ให้ตอบ qid เป็น null — การไม่จับคู่ดีกว่าจับคู่ผิด
เพราะเลขที่ผิดจะติดอยู่กับตัวตนนั้นไปตลอดโดยไม่มีใครรู้

ตอบ JSON เดียวเท่านั้น:
{"qid": "Q123" หรือ null, "confidence": 0.0-1.0, "reason": "เหตุผลสั้น ๆ ภาษาไทย"}"""

WIKIDATA_USER = """ชื่อในข่าว: {name}
ประเภทที่ระบบจัดไว้: {entity_type}
{context}
ตัวเลือกจาก Wikidata:
{candidates}"""


def wikidata_messages(
    name: str,
    candidates: list[tuple[str, str, str]],
    *,
    entity_type: str = "unknown",
    context: str | None = None,
) -> list[dict[str, str]]:
    """Build the linking prompt.

    The article summary is what separates the country from the empire, so it is
    passed whenever there is one; without it the model is choosing on the name
    alone, which is exactly the mistake this step exists to avoid.
    """
    listing = "\n".join(
        f"- {qid}: {label}" + (f" — {description}" if description else " — (ไม่มีคำอธิบาย)")
        for qid, label, description in candidates
    )
    prefix = f"บริบทของข่าว: {context.strip()}\n" if context and context.strip() else ""
    return [
        {"role": "system", "content": WIKIDATA_SYSTEM},
        {
            "role": "user",
            "content": WIKIDATA_USER.format(
                name=name, entity_type=entity_type, context=prefix, candidates=listing
            ),
        },
    ]
