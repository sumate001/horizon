from datetime import UTC, datetime

import pytest

from horizon.llm.prompts import extraction_messages
from horizon.pipeline.extract import (
    BANGKOK,
    SUMMARY_LIMIT,
    ExtractionError,
    extract_event,
    normalize,
    parse_event_time,
)

GOOD_PAYLOAD = {
    "actors": ["ธนาคารแห่งประเทศไทย (BOT)", "คณะกรรมการนโยบายการเงิน (MPC)"],
    "action": "คงอัตราดอกเบี้ยนโยบายที่ 2.25%",
    "location": "กรุงเทพมหานคร",
    "time": "2026-08-22T14:30:00+07:00",
    "categories": ["เศรษฐกิจ", "การเมือง"],
    "summary": "กนง. มีมติเอกฉันท์คงดอกเบี้ยนโยบายที่ 2.25%",
    "confidence": 0.91,
}


class FakeOllama:
    """Returns queued responses in order; raises if asked for more than queued."""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def chat_raw(self, messages, *, purpose="chat", model=None):
        self.calls.append(purpose)
        if not self.responses:
            raise AssertionError("unexpected extra LLM call")
        return self.responses.pop(0)


# ── time parsing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-22T14:30:00+07:00", datetime(2026, 8, 22, 7, 30, tzinfo=UTC)),
        ("2026-08-22T07:30:00Z", datetime(2026, 8, 22, 7, 30, tzinfo=UTC)),
        ("2026-08-22 14:30:00", datetime(2026, 8, 22, 7, 30, tzinfo=UTC)),
    ],
)
def test_parse_event_time_normalizes_to_utc(value, expected):
    assert parse_event_time(value) == expected


def test_naive_dates_are_read_as_bangkok_local():
    assert parse_event_time("2026-08-22") == datetime(2026, 8, 22, tzinfo=BANGKOK).astimezone(
        UTC
    )


@pytest.mark.parametrize("value", [None, "", "   ", "วันนี้", "not a date", 42, []])
def test_unparseable_times_become_none(value):
    """The prompt forbids guessing dates — junk must not fail the article."""
    assert parse_event_time(value) is None


# ── normalization ────────────────────────────────────────────────────────────


def test_normalize_maps_a_clean_payload():
    result = normalize(GOOD_PAYLOAD)
    assert result.actors == GOOD_PAYLOAD["actors"]
    assert result.action == GOOD_PAYLOAD["action"]
    assert result.location == "กรุงเทพมหานคร"
    assert result.categories == ["เศรษฐกิจ", "การเมือง"]
    assert result.confidence == pytest.approx(0.91)
    assert result.incomplete is False


def test_normalize_drops_categories_outside_the_closed_list():
    result = normalize({**GOOD_PAYLOAD, "categories": ["เศรษฐกิจ", "หมวดที่แต่งขึ้น"]})
    assert result.categories == ["เศรษฐกิจ"]


def test_normalize_caps_categories_at_three():
    result = normalize(
        {
            **GOOD_PAYLOAD,
            "categories": ["เศรษฐกิจ", "การเมือง", "สังคม", "เทคโนโลยี", "พลังงาน"],
        }
    )
    assert len(result.categories) == 3


def test_normalize_dedupes_actors_and_categories():
    result = normalize(
        {**GOOD_PAYLOAD, "actors": ["ก", "ก", " ก ", "ข"], "categories": ["สังคม", "สังคม"]}
    )
    assert result.actors == ["ก", "ข"]
    assert result.categories == ["สังคม"]


def test_normalize_truncates_summary_and_clamps_confidence():
    result = normalize({**GOOD_PAYLOAD, "summary": "ก" * 400, "confidence": 5})
    assert len(result.summary) == SUMMARY_LIMIT
    assert result.confidence == 1.0


def test_normalize_survives_garbage_field_types():
    result = normalize(
        {"actors": "not a list", "action": 5, "categories": None, "confidence": "abc"}
    )
    assert result.actors == []
    assert result.action is None
    assert result.categories == []
    assert result.confidence == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        {**GOOD_PAYLOAD, "actors": []},
        {**GOOD_PAYLOAD, "action": None},
    ],
)
def test_missing_actors_or_action_marks_the_event_incomplete(payload):
    assert normalize(payload).incomplete is True


# ── the LLM round trip ───────────────────────────────────────────────────────


async def test_extract_event_parses_a_valid_response():
    import json

    client = FakeOllama(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    result = await extract_event("หัวข้อ", "เนื้อหา", client=client)
    assert result.action == GOOD_PAYLOAD["action"]
    assert client.calls == ["extract"]


async def test_extract_event_strips_qwen_thinking_blocks():
    import json

    body = f"<think>ลองคิดดูก่อน</think>{json.dumps(GOOD_PAYLOAD, ensure_ascii=False)}"
    result = await extract_event("หัวข้อ", "เนื้อหา", client=FakeOllama(body))
    assert result.confidence == pytest.approx(0.91)


async def test_extract_event_repairs_malformed_json_once():
    import json

    client = FakeOllama("นี่ไม่ใช่ JSON เลย", json.dumps(GOOD_PAYLOAD, ensure_ascii=False))
    result = await extract_event("หัวข้อ", "เนื้อหา", client=client)
    assert result.action == GOOD_PAYLOAD["action"]
    assert client.calls == ["extract", "extract_repair"]


async def test_extract_event_raises_when_repair_also_fails():
    client = FakeOllama("ยังไม่ใช่ JSON", "ก็ยังไม่ใช่อยู่ดี")
    with pytest.raises(ExtractionError):
        await extract_event("หัวข้อ", "เนื้อหา", client=client)
    assert client.calls == ["extract", "extract_repair"]


# ── the publication-date anchor ──────────────────────────────────────────────


def test_prompt_carries_the_publication_date_in_bangkok_time():
    """Thai text writes "21 ส.ค." with no year; without this anchor the model
    invents one, which then skews the temporal feature during clustering."""
    published = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)  # 03:00 on the 22nd in Bangkok
    prompt = extraction_messages("หัวข้อ", "เนื้อหา", published_at=published)[1]["content"]
    assert "วันที่เผยแพร่ข่าว: 2026-08-22" in prompt


def test_prompt_says_unknown_when_no_publication_date():
    prompt = extraction_messages("หัวข้อ", "เนื้อหา")[1]["content"]
    assert "วันที่เผยแพร่ข่าว: ไม่ทราบ" in prompt
