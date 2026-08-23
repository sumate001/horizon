import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from horizon.llm.prompts import pairwise_messages, scenario_messages, uncertainty_messages
from horizon.reasoner.alerts import AlertContent
from horizon.reasoner.dispatch import SignalRef
from horizon.reasoner.forces import (
    DEFAULT_UNCERTAINTY,
    UNCERTAINTY_LEVELS,
    ForceResult,
    PairwiseJudge,
)
from horizon.reasoner.scenario import (
    DEFAULT_WATCH_TYPE,
    MAX_INDICATORS,
    ScenarioContext,
    normalize_indicators,
)
from horizon.services.reasoner import parse_signal

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


class FakeOllama:
    """Returns queued responses; records the purpose of every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def chat_json(self, messages, *, purpose="chat", model=None):
        self.calls.append(purpose)
        if not self.responses:
            raise AssertionError("unexpected extra LLM call")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# ── pairwise judging ─────────────────────────────────────────────────────────


async def test_the_judge_reads_an_a_answer_as_a_win_for_the_left_side():
    judge = PairwiseJudge(FakeOllama({"choice": "A"}))
    verdict = await judge.judge(
        force_id=uuid.uuid4(),
        force_name="เศรษฐกิจ",
        force_definition="นิยาม",
        left=("id-a", "เรื่อง ก"),
        right=("id-b", "เรื่อง ข"),
    )
    assert verdict is True


async def test_an_unparseable_answer_leaves_the_pair_unjudged():
    """A skipped pair reads as "no preference" in AHP; guessing a winner would
    put a fabricated preference into the matrix."""
    judge = PairwiseJudge(FakeOllama({"choice": "maybe both"}))
    verdict = await judge.judge(
        force_id=uuid.uuid4(),
        force_name="เศรษฐกิจ",
        force_definition="นิยาม",
        left=("id-a", "ก"),
        right=("id-b", "ข"),
    )
    assert verdict is None
    assert judge.failures == 1


async def test_the_judge_caches_a_comparison():
    llm = FakeOllama({"choice": "A"})
    judge = PairwiseJudge(llm)
    force_id = uuid.uuid4()
    args = {
        "force_id": force_id,
        "force_name": "เศรษฐกิจ",
        "force_definition": "นิยาม",
        "left": ("id-a", "ก"),
        "right": ("id-b", "ข"),
    }
    assert await judge.judge(**args) is True
    assert await judge.judge(**args) is True  # no second call queued
    assert len(llm.calls) == 1


async def test_the_cache_answers_the_reversed_pair_too():
    """A beats B implies B does not beat A — asking again would waste a call."""
    llm = FakeOllama({"choice": "A"})
    judge = PairwiseJudge(llm)
    force_id = uuid.uuid4()
    await judge.judge(
        force_id=force_id,
        force_name="เศรษฐกิจ",
        force_definition="นิยาม",
        left=("id-a", "ก"),
        right=("id-b", "ข"),
    )
    reversed_verdict = await judge.judge(
        force_id=force_id,
        force_name="เศรษฐกิจ",
        force_definition="นิยาม",
        left=("id-b", "ข"),
        right=("id-a", "ก"),
    )
    assert reversed_verdict is False
    assert len(llm.calls) == 1


def test_the_uncertainty_levels_match_the_spec():
    assert UNCERTAINTY_LEVELS == {"low": 0.2, "medium": 0.5, "high": 0.8}
    assert DEFAULT_UNCERTAINTY == 0.5


@pytest.mark.parametrize("ratio,expected", [(0.0, True), (0.1, True), (0.11, False)])
def test_a_force_result_reports_whether_its_comparisons_held_together(ratio, expected):
    result = ForceResult(
        force_id=uuid.uuid4(),
        force_name="เศรษฐกิจ",
        impact=0.5,
        uncertainty=0.5,
        ahp_rank=1,
        consistency_ratio=ratio,
        comparisons=15,
    )
    assert result.trustworthy is expected


# ── prompts ──────────────────────────────────────────────────────────────────


def test_the_pairwise_prompt_names_both_sides_and_the_force_definition():
    content = pairwise_messages("เศรษฐกิจ", "นิยามของแรง", "เรื่อง ก", "เรื่อง ข")[1]["content"]
    assert "นิยามของแรง" in content
    assert "เรื่อง ก" in content and "เรื่อง ข" in content


def test_the_uncertainty_prompt_survives_a_cluster_with_no_summaries():
    content = uncertainty_messages("เศรษฐกิจ", "นิยาม", "หัวข้อ", [])[1]["content"]
    assert "ไม่มีข้อมูล" in content


def test_the_scenario_prompt_forbids_reading_it_as_a_forecast():
    system = scenario_messages(
        label="หัวข้อ", events=["เหตุการณ์"], forces=[], trend=[], related=[]
    )[0]["content"]
    assert "ไม่ใช่คำพยากรณ์" in system


def test_the_scenario_prompt_carries_every_context_block():
    content = scenario_messages(
        label="หัวข้อทดสอบ",
        events=["เหตุการณ์ ก", "เหตุการณ์ ข"],
        forces=["เศรษฐกิจ: impact 0.80, uncertainty 0.50"],
        trend=[1.0, 2.5],
        related=["กลุ่มใกล้เคียง (ความคล้าย 0.91)"],
    )[1]["content"]
    assert "หัวข้อทดสอบ" in content
    assert "เหตุการณ์ ก" in content
    assert "impact 0.80" in content
    assert "1.00, 2.50" in content
    assert "กลุ่มใกล้เคียง" in content


# ── scenario output handling ─────────────────────────────────────────────────


def test_indicators_keep_a_recognised_watch_type():
    indicators = normalize_indicators(
        [{"description": "ราคาน้ำมันดิบ", "watch_type": "เศรษฐกิจ"}]
    )
    assert indicators == [{"description": "ราคาน้ำมันดิบ", "watch_type": "เศรษฐกิจ"}]


def test_an_invented_watch_type_falls_back_instead_of_being_stored():
    indicators = normalize_indicators([{"description": "อะไรสักอย่าง", "watch_type": "ดวงจันทร์"}])
    assert indicators[0]["watch_type"] == DEFAULT_WATCH_TYPE


def test_indicators_without_a_description_are_dropped():
    assert normalize_indicators([{"watch_type": "เศรษฐกิจ"}, {"description": "  "}]) == []


def test_indicators_are_capped():
    many = [{"description": f"ตัวชี้วัด {i}", "watch_type": "สังคม"} for i in range(20)]
    assert len(normalize_indicators(many)) == MAX_INDICATORS


@pytest.mark.parametrize("raw", [None, "ข้อความ", 42, [1, 2, 3]])
def test_garbage_indicators_do_not_crash_the_scenario(raw):
    assert normalize_indicators(raw) == []


def test_a_cluster_with_one_event_is_not_enough_for_a_scenario():
    context = ScenarioContext(
        label="หัวข้อ", event_ids=[uuid.uuid4()], summaries=["เหตุการณ์เดียว"],
        forces=[], trend=[], related=[],
    )
    assert context.usable is False


def test_two_events_are_enough():
    context = ScenarioContext(
        label="หัวข้อ", event_ids=[uuid.uuid4()] * 2, summaries=["ก", "ข"],
        forces=[], trend=[], related=[],
    )
    assert context.usable is True


# ── signal parsing ───────────────────────────────────────────────────────────


def test_a_weak_signal_message_parses():
    cluster_id, ref_id = uuid.uuid4(), uuid.uuid4()
    parsed = parse_signal(
        json.dumps(
            {
                "signal_type": "weak_signal",
                "ref_id": str(ref_id),
                "cluster_id": str(cluster_id),
                "title": "หัวข้อ",
                "combined_score": 0.71,
            }
        )
    )
    assert parsed is not None
    assert parsed.signal_type == "weak_signal"
    assert parsed.ref_id == ref_id
    assert parsed.cluster_id == cluster_id
    assert parsed.combined_score == pytest.approx(0.71)


def test_a_trend_breakout_message_parses():
    parsed = parse_signal(
        json.dumps(
            {
                "signal_type": "trend_breakout",
                "ref_id": str(uuid.uuid4()),
                "trend_score": 3.4,
            }
        )
    )
    assert parsed is not None
    assert parsed.trend_score == pytest.approx(3.4)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        json.dumps({"signal_type": "rumour", "ref_id": str(uuid.uuid4())}),
        json.dumps({"signal_type": "weak_signal"}),
        json.dumps({"signal_type": "weak_signal", "ref_id": "not-a-uuid"}),
    ],
)
def test_an_unusable_message_is_dropped_rather_than_crashing_the_subscriber(raw):
    assert parse_signal(raw) is None


def test_a_junk_cluster_id_does_not_reject_an_otherwise_valid_signal():
    parsed = parse_signal(
        json.dumps(
            {"signal_type": "weak_signal", "ref_id": str(uuid.uuid4()), "cluster_id": "junk"}
        )
    )
    assert parsed is not None
    assert parsed.cluster_id is None


# ── alert text ───────────────────────────────────────────────────────────────


def _alert(**kwargs) -> AlertContent:
    base = {
        "signal_type": "weak_signal",
        "title": "สัญญาณอ่อนด้านพลังงาน",
        "score_label": "คะแนนรวม",
        "score": 0.72,
        "summary": "พบรายงานไฟฟ้าดับซ้ำในนิคมอุตสาหกรรม",
        "categories": ["พลังงาน", "เศรษฐกิจ"],
        "dashboard_url": "http://localhost:8301/weak-signals",
    }
    base.update(kwargs)
    return AlertContent(**base)


def test_the_alert_carries_type_title_score_and_link():
    text = _alert().as_text()
    assert "สัญญาณอ่อน" in text
    assert "สัญญาณอ่อนด้านพลังงาน" in text
    assert "0.72" in text
    assert "http://localhost:8301/weak-signals" in text


def test_a_trend_breakout_alert_says_so_in_thai():
    assert "แนวโน้มพุ่งผิดปกติ" in _alert(signal_type="trend_breakout").as_text()


def test_an_alert_without_categories_or_summary_still_renders():
    text = _alert(categories=[], summary="").as_text()
    assert "สัญญาณอ่อนด้านพลังงาน" in text
    assert "หมวด:" not in text


# ── contract ─────────────────────────────────────────────────────────────────


def test_a_dispatch_payload_validates_against_the_shared_schema():
    """The payload built by the reasoner is what phase 4 POSTs to OSINT//DESK.

    Building it correctly now means the webhook client has nothing left to do
    but send it — and this catches drift in either repo's field names.
    """
    schema = json.loads((CONTRACTS / "signal_inbound.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )

    payload = {
        "signal_id": str(uuid.uuid4()),
        "signal_type": "weak_signal",
        "title": "สัญญาณอ่อนด้านพลังงานในภาคตะวันออก",
        "combined_score": 0.72,
        "trend_score": 0.0,
        "categories": ["พลังงาน", "เศรษฐกิจ"],
        "summary": "พบรายงานไฟฟ้าดับซ้ำในนิคมอุตสาหกรรมหลายแห่ง",
        "top_events": [
            {
                "summary": "โรงไฟฟ้าหยุดเดินเครื่องฉุกเฉิน",
                "url": "https://example.com/a",
                "source_name": "Thai PBS",
                "credibility_weight": 0.9,
                "event_time": datetime(2026, 8, 22, 7, 30, tzinfo=UTC).isoformat(),
            },
            {
                "summary": "นิคมรายงานไฟตกซ้ำ",
                "url": "https://example.com/b",
                "source_name": "ประชาไท",
                "credibility_weight": 0.75,
                "event_time": None,
            },
        ],
        "force_assessments": [
            {"force": "เศรษฐกิจและการเงิน", "impact": 1.0, "uncertainty": 0.5}
        ],
        "cluster_id": str(uuid.uuid4()),
        "scenario_id": str(uuid.uuid4()),
        "created_at": datetime(2026, 8, 22, 8, 0, tzinfo=UTC).isoformat(),
    }
    validator.validate(payload)


def test_the_signal_ref_carries_both_shapes_a_signal_can_take():
    """Weak signals point at an event or a cluster; breakouts only at a cluster."""
    cluster_only = SignalRef(
        signal_type="trend_breakout",
        ref_id=uuid.uuid4(),
        cluster_id=uuid.uuid4(),
        event_id=None,
        title="หัวข้อ",
        combined_score=0.0,
        trend_score=3.1,
    )
    assert cluster_only.event_id is None
    assert cluster_only.cluster_id is not None
