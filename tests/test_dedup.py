import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from horizon.pipeline.dedup import (
    Candidate,
    Deduplicator,
    MinHashIndex,
    build_minhash,
    classify_match,
    numbers_in,
    shingles,
)
from horizon.pipeline.extract import Extraction
from horizon.pipeline.vectors import VectorHit

# Realistic article length matters here: with a 100-character body a headline
# swap alone drops Jaccard below 0.85, which would make these fixtures test the
# fixture rather than the threshold.
THAI_A = (
    "ธนาคารแห่งประเทศไทยประกาศคงอัตราดอกเบี้ยนโยบายไว้ที่ 2.25% "
    "ในการประชุมคณะกรรมการนโยบายการเงินวันนี้ "
    "โดยระบุว่าเศรษฐกิจไทยฟื้นตัวต่อเนื่องตามการบริโภคภาคเอกชนและภาคการท่องเที่ยวที่ขยายตัวดีกว่าคาด "
    "ขณะที่อัตราเงินเฟ้อทั่วไปยังอยู่ในกรอบเป้าหมาย คณะกรรมการมีมติเป็นเอกฉันท์ 7 ต่อ 0 เสียง "
    "และประเมินว่าการส่งออกจะทยอยฟื้นตัวในครึ่งปีหลัง "
    "แม้ยังมีความเสี่ยงจากเศรษฐกิจโลกและความขัดแย้งทางภูมิรัฐศาสตร์ "
    "ผู้ว่าการธนาคารแห่งประเทศไทยกล่าวเพิ่มเติมว่า นโยบายการเงินในปัจจุบันอยู่ในระดับที่เหมาะสมกับแนวโน้มเศรษฐกิจ "
    "และพร้อมปรับเปลี่ยนหากภาวะเศรษฐกิจการเงินเปลี่ยนแปลงอย่างมีนัยสำคัญ"
)
THAI_A_REWORDED = (
    THAI_A.replace("โดยระบุว่า", "โดยชี้ว่า")
    .replace("ฟื้นตัวต่อเนื่อง", "ฟื้นตัวอย่างต่อเนื่อง")
    .replace("ดีกว่าคาด", "สูงกว่าที่คาดการณ์")
)
THAI_B = (
    "กรมอุตุนิยมวิทยาเตือนพายุฝนฟ้าคะนองในภาคเหนือตอนบนและภาคตะวันออกเฉียงเหนือ "
    "ขอให้ประชาชนระวังอันตรายจากฝนตกหนักถึงหนักมาก ลมกระโชกแรง "
    "และน้ำท่วมฉับพลันในช่วงสามวันข้างหน้า โดยเฉพาะพื้นที่ลาดเชิงเขาและที่ราบลุ่ม"
)


# ── shingling / minhash ──────────────────────────────────────────────────────


def test_shingles_handles_short_thai_text():
    assert shingles("สั้น") == {"สั้น"}
    assert shingles("") == set()


def test_shingles_are_character_ngrams():
    assert shingles("abcdefg", k=5) == {"abcde", "bcdef", "cdefg"}


def test_minhash_syndicated_repost_scores_above_threshold():
    """Same wire copy under a different headline — the case L1 exists for."""
    a = build_minhash("กนง.คงดอกเบี้ยนโยบาย", THAI_A)
    b = build_minhash("แบงก์ชาติคงดอกเบี้ย 2.25%", THAI_A)
    assert a.jaccard(b) >= 0.85


def test_minhash_reworded_text_falls_below_threshold():
    """A rewrite is L2's problem: character shingles move too much for L1."""
    a = build_minhash("ดอกเบี้ยนโยบาย", THAI_A)
    b = build_minhash("ดอกเบี้ยนโยบาย", THAI_A_REWORDED)
    assert a.jaccard(b) < 0.85


def test_minhash_unrelated_text_scores_low():
    a = build_minhash("ดอกเบี้ยนโยบาย", THAI_A)
    b = build_minhash("พายุฝนฟ้าคะนอง", THAI_B)
    assert a.jaccard(b) < 0.2


def test_minhash_signature_round_trips_through_bytes():
    original = build_minhash("หัวข้อ", THAI_A)
    restored = Candidate(
        event_id=uuid.uuid4(),
        summary="",
        event_time=None,
        credibility_weight=0.5,
        minhash_sig=original.hashvalues.tobytes(),
    ).minhash()
    assert restored is not None
    assert original.jaccard(restored) == 1.0


# ── duplicate vs update ──────────────────────────────────────────────────────


def _candidate(summary: str, event_time: datetime | None = None) -> Candidate:
    return Candidate(
        event_id=uuid.uuid4(),
        summary=summary,
        event_time=event_time,
        credibility_weight=0.6,
    )


def test_numbers_in_reads_thai_and_arabic_digits():
    assert numbers_in("ผู้เสียชีวิต 12 ราย บาดเจ็บ ๓๔ ราย") == {"12", "๓๔"}
    assert numbers_in("ไม่มีตัวเลข") == set()


def test_identical_fields_are_a_duplicate():
    extraction = Extraction(summary="ผู้เสียชีวิต 12 ราย", event_time=None)
    action, _ = classify_match(extraction, _candidate("ผู้เสียชีวิต 12 ราย"))
    assert action == "duplicate"


def test_changed_number_is_an_update():
    extraction = Extraction(summary="ผู้เสียชีวิต 15 ราย", event_time=None)
    action, reason = classify_match(extraction, _candidate("ผู้เสียชีวิต 12 ราย"))
    assert action == "update"
    assert "numeric" in reason


def test_changed_event_time_is_an_update():
    when = datetime(2026, 8, 22, tzinfo=timezone.utc)
    extraction = Extraction(summary="เหมือนเดิม", event_time=when)
    action, reason = classify_match(extraction, _candidate("เหมือนเดิม", event_time=None))
    assert action == "update"
    assert "event_time" in reason


def test_reworded_summary_without_new_facts_is_a_duplicate():
    extraction = Extraction(summary="ยอดผู้เสียชีวิตอยู่ที่ 12 ราย", event_time=None)
    action, _ = classify_match(extraction, _candidate("ผู้เสียชีวิต 12 ราย"))
    assert action == "duplicate"


# ── Deduplicator ─────────────────────────────────────────────────────────────


@dataclass
class FakeVectorStore:
    hits: list[VectorHit]
    deleted: list[uuid.UUID]

    async def search(self, vector, *, limit=10, since=None):
        return self.hits

    async def delete_event(self, event_id):
        self.deleted.append(event_id)


def _dedup(hits=None, candidates=None):
    store = FakeVectorStore(hits=hits or [], deleted=[])
    lookup = candidates or {}

    async def load(event_id):
        return lookup.get(event_id)

    return Deduplicator(MinHashIndex(redis_url=None), store, load), store


async def test_unseen_article_is_new():
    dedup, _ = _dedup()
    result = await dedup.check(
        title="พายุ", body=THAI_B, extraction=Extraction(summary=THAI_B), embedding=[0.1] * 8
    )
    assert result.action == "new"


async def test_l1_catches_a_syndicated_repost():
    existing_id = uuid.uuid4()
    minhash = build_minhash("กนง.คงดอกเบี้ยนโยบาย", THAI_A)
    candidate = Candidate(
        event_id=existing_id,
        summary=THAI_A,
        event_time=None,
        credibility_weight=0.9,
        minhash_sig=minhash.hashvalues.tobytes(),
    )
    dedup, _ = _dedup(candidates={existing_id: candidate})
    dedup.register(existing_id, minhash)

    result = await dedup.check(
        title="แบงก์ชาติคงดอกเบี้ย 2.25%",
        body=THAI_A,
        extraction=Extraction(summary=THAI_A),
        embedding=[0.1] * 8,
    )
    assert result.action == "duplicate"
    assert result.layer == "l1"
    assert result.similarity >= 0.85


async def test_l1_lets_a_reworded_story_through_to_l2():
    existing_id = uuid.uuid4()
    minhash = build_minhash("ดอกเบี้ยนโยบาย", THAI_A)
    dedup, _ = _dedup(
        candidates={existing_id: _candidate_with_id(existing_id, THAI_A)}
    )
    dedup.register(existing_id, minhash)

    result = await dedup.check(
        title="ดอกเบี้ยนโยบาย",
        body=THAI_A_REWORDED,
        extraction=Extraction(summary=THAI_A_REWORDED),
        embedding=[],
    )
    assert result.action == "new"  # no vector hits supplied, so L2 finds nothing


async def test_l1_index_entry_without_its_event_is_evicted():
    ghost_id = uuid.uuid4()
    minhash = build_minhash("ดอกเบี้ย", THAI_A)
    dedup, _ = _dedup()
    dedup.register(ghost_id, minhash)

    result = await dedup.check(
        title="ดอกเบี้ย", body=THAI_A, extraction=Extraction(summary=THAI_A), embedding=[]
    )
    assert result.action == "new"
    assert dedup.index.query(minhash) == []


async def test_l2_duplicate_above_cosine_threshold():
    existing_id = uuid.uuid4()
    dedup, _ = _dedup(
        hits=[VectorHit(event_id=existing_id, score=0.94, payload={})],
        candidates={existing_id: _candidate_with_id(existing_id, "ผู้เสียชีวิต 12 ราย")},
    )
    result = await dedup.check(
        title="อุบัติเหตุ",
        body=THAI_B,
        extraction=Extraction(summary="ผู้เสียชีวิต 12 ราย"),
        embedding=[0.2] * 8,
    )
    assert result.action == "duplicate"
    assert result.layer == "l2"
    assert result.similarity == pytest.approx(0.94)


async def test_l2_update_when_numbers_moved():
    existing_id = uuid.uuid4()
    dedup, _ = _dedup(
        hits=[VectorHit(event_id=existing_id, score=0.91, payload={})],
        candidates={existing_id: _candidate_with_id(existing_id, "ผู้เสียชีวิต 12 ราย")},
    )
    result = await dedup.check(
        title="อุบัติเหตุ",
        body=THAI_B,
        extraction=Extraction(summary="ผู้เสียชีวิต 19 ราย"),
        embedding=[0.2] * 8,
    )
    assert result.action == "update"
    assert result.layer == "l2"


async def test_l2_below_threshold_is_new():
    existing_id = uuid.uuid4()
    dedup, _ = _dedup(
        hits=[VectorHit(event_id=existing_id, score=0.80, payload={})],
        candidates={existing_id: _candidate_with_id(existing_id, "ผู้เสียชีวิต 12 ราย")},
    )
    result = await dedup.check(
        title="อุบัติเหตุ",
        body=THAI_B,
        extraction=Extraction(summary="ผู้เสียชีวิต 12 ราย"),
        embedding=[0.2] * 8,
    )
    assert result.action == "new"


async def test_l2_drops_a_vector_whose_event_is_gone():
    ghost_id = uuid.uuid4()
    dedup, store = _dedup(hits=[VectorHit(event_id=ghost_id, score=0.95, payload={})])
    result = await dedup.check(
        title="อุบัติเหตุ",
        body=THAI_B,
        extraction=Extraction(summary="ผู้เสียชีวิต 12 ราย"),
        embedding=[0.2] * 8,
    )
    assert result.action == "new"
    assert store.deleted == [ghost_id]


def _candidate_with_id(event_id: uuid.UUID, summary: str) -> Candidate:
    return Candidate(
        event_id=event_id, summary=summary, event_time=None, credibility_weight=0.6
    )
