"""Idempotent seed: the six PESTEL driving forces and a starter source registry.

Runs after every migration (`docker compose run --rm migrate`). Existing rows are
left alone — credibility weights tuned by an analyst survive redeploys.
"""

import asyncio
import logging

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .logging import setup_logging
from .models import DrivingForce, Source

log = logging.getLogger("horizon.seed")

# dimension E2 is PESTEL's second "E" (Environmental); E is Economic.
DRIVING_FORCES = [
    (
        "การเมืองและนโยบายรัฐ",
        "P",
        "เสถียรภาพรัฐบาล การเปลี่ยนขั้วอำนาจ นโยบายสาธารณะ การเลือกตั้ง "
        "และความสัมพันธ์ระหว่างประเทศที่กระทบการตัดสินใจเชิงนโยบายของไทย",
    ),
    (
        "เศรษฐกิจและการเงิน",
        "E",
        "อัตราการเติบโต เงินเฟ้อ อัตราดอกเบี้ย ค่าเงิน การค้าและการลงทุน "
        "ต้นทุนพลังงาน และเสถียรภาพของภาคการเงิน",
    ),
    (
        "สังคมและประชากร",
        "S",
        "โครงสร้างประชากร แรงงาน ความเหลื่อมล้ำ การศึกษา สาธารณสุข "
        "ค่านิยม และการเคลื่อนไหวทางสังคม",
    ),
    (
        "เทคโนโลยีและนวัตกรรม",
        "T",
        "เทคโนโลยีเกิดใหม่ ปัญญาประดิษฐ์ ความมั่นคงไซเบอร์ โครงสร้างพื้นฐานดิจิทัล "
        "และการเปลี่ยนผ่านสู่ดิจิทัลของภาครัฐและเอกชน",
    ),
    (
        "สิ่งแวดล้อมและภูมิอากาศ",
        "E2",
        "การเปลี่ยนแปลงสภาพภูมิอากาศ ภัยพิบัติ มลพิษ ทรัพยากรน้ำ "
        "และการเปลี่ยนผ่านด้านพลังงานสะอาด",
    ),
    (
        "กฎหมายและกฎระเบียบ",
        "L",
        "การออกและแก้ไขกฎหมาย การกำกับดูแลภาคธุรกิจ กระบวนการยุติธรรม "
        "และมาตรฐานระหว่างประเทศที่มีผลผูกพัน",
    ),
]

# Starter registry, all verified to return entries. Credibility weights are an
# initial estimate — tune them via PATCH /api/v1/sources or the Sources page.
#
# Deliberately absent: Thai PBS and PPTV HD36 publish no reachable RSS (404 on
# every documented path) and nationthailand.com/rss serves an HTML page. Add them
# back through the API once a working feed URL exists — a dead feed just logs a
# failure every poll.
SOURCES = [
    ("BBC Thai", "https://feeds.bbci.co.uk/thai/rss.xml", "rss", 0.90),
    ("ThaiPublica", "https://thaipublica.org/feed/", "rss", 0.85),
    ("Bangkok Post", "https://www.bangkokpost.com/rss/data/topstories.xml", "rss", 0.85),
    ("สำนักข่าวอิศรา", "https://www.isranews.org/isranews.feed?type=rss", "rss", 0.80),
    ("The Standard", "https://thestandard.co/feed/", "rss", 0.80),
    ("ประชาไท", "https://prachatai.com/rss.xml", "rss", 0.75),
    ("มติชนออนไลน์", "https://www.matichon.co.th/feed", "rss", 0.75),
    ("ไทยรัฐ", "https://www.thairath.co.th/rss/news", "rss", 0.70),
    ("ข่าวสด", "https://www.khaosod.co.th/feed", "rss", 0.70),
    # International. The extraction prompt already expects mixed Thai/English
    # input and writes its summary in Thai, so these land in the same pipeline,
    # the same categories (ต่างประเทศ is one of them), and reach OSINT//DESK in
    # Thai like everything else — no separate path.
    #
    # Weighted toward Asia-Pacific on purpose: a Thai newsroom needs the region
    # it reports on before it needs another wire on Washington.
    #
    # Deliberately absent: Reuters and AP. Neither runs a working public feed
    # any more — reutersagency.com 404s, feeds.reuters.com no longer resolves,
    # and every AP path tested either refuses to connect or returns 403. They
    # are the two most worth having, so if a feed URL ever works again, these
    # are the first to add back.
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "rss", 0.90),
    ("CNA", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", "rss", 0.85),
    ("Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar", "rss", 0.85),
    ("NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "rss", 0.85),
    ("DW World", "https://rss.dw.com/rdf/rss-en-world", "rss", 0.85),
    ("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "rss", 0.85),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "rss", 0.80),
    ("The Guardian World", "https://www.theguardian.com/world/rss", "rss", 0.80),
    # Analysis rather than straight reporting, and a state-linked owner
    # respectively — both useful, both worth reading at a discount.
    ("The Diplomat", "https://thediplomat.com/feed/", "rss", 0.75),
    ("South China Morning Post", "https://www.scmp.com/rss/91/feed", "rss", 0.75),
]


async def seed() -> None:
    async with session_scope() as session:
        existing_forces = set(
            (await session.execute(select(DrivingForce.name))).scalars()
        )
        added_forces = 0
        for name, dimension, definition in DRIVING_FORCES:
            if name in existing_forces:
                continue
            session.add(
                DrivingForce(
                    name=name, dimension=dimension, definition=definition, weight=1.0, active=True
                )
            )
            added_forces += 1

        existing_sources = set((await session.execute(select(Source.url))).scalars())
        added_sources = 0
        for name, url, source_type, credibility in SOURCES:
            if url in existing_sources:
                continue
            session.add(
                Source(
                    name=name,
                    url=url,
                    type=source_type,
                    credibility_weight=credibility,
                    active=True,
                )
            )
            added_sources += 1

    log.info(
        "seed complete",
        extra={"driving_forces_added": added_forces, "sources_added": added_sources},
    )


if __name__ == "__main__":
    setup_logging("horizon.seed", get_settings().log_level)
    asyncio.run(seed())
