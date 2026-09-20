"""Contract tests against the JSON Schema files shared with OSINT//DESK.

`contracts/` is the source of truth for both repos. These tests fail loudly if a
field is renamed on one side only. Phase 4 wires the real payload builder into
`valid_signal()`; until then the samples stand in for it.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validator(filename: str) -> Draft202012Validator:
    schema = load(filename)
    Draft202012Validator.check_schema(schema)
    # Without the format checker, `format: uuid` / `date-time` are annotations
    # only — the contract would accept "not-a-uuid" and drift would go unnoticed.
    return Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


@pytest.fixture(scope="module")
def signal_validator() -> Draft202012Validator:
    return _validator("signal_inbound.schema.json")


@pytest.fixture(scope="module")
def verdict_validator() -> Draft202012Validator:
    return _validator("verdict.schema.json")


def test_date_time_format_checking_is_active(verdict_validator):
    """Guards the rfc3339-validator dependency: without it, date-time is skipped."""
    with pytest.raises(ValidationError):
        verdict_validator.validate(valid_verdict(closed_at="เมื่อวานนี้"))


def valid_signal(**overrides) -> dict:
    payload = {
        "signal_id": "3f1a1c8e-4a2b-4f31-9c1e-8b0f2a6d7e55",
        "signal_type": "weak_signal",
        "title": "สัญญาณอ่อนด้านพลังงานในภาคตะวันออก",
        "combined_score": 0.72,
        "trend_score": 1.8,
        "categories": ["พลังงาน", "เศรษฐกิจ"],
        "summary": "พบรายงานการหยุดจ่ายไฟฟ้าในนิคมอุตสาหกรรมหลายแห่งภายในสัปดาห์เดียว",
        "top_events": [
            {
                "summary": "โรงไฟฟ้าแห่งหนึ่งหยุดเดินเครื่องฉุกเฉิน",
                "url": "https://example.com/a",
                "source_name": "Thai PBS",
                "credibility_weight": 0.9,
                "event_time": "2026-08-22T07:30:00+00:00",
            },
            {
                "summary": "นิคมอุตสาหกรรมรายงานไฟตกซ้ำ",
                "url": "https://example.com/b",
                "source_name": "ประชาไท",
                "credibility_weight": 0.75,
                "event_time": None,
            },
        ],
        "force_assessments": [
            {"force": "เศรษฐกิจและการเงิน", "impact": 0.66, "uncertainty": 0.5}
        ],
        "cluster_id": None,
        # Always present, null on a detection — the same convention cluster_id
        # follows, so a receiver never has to tell "absent" from "not set".
        "beat_id": None,
        "beat_name": None,
        "beat_reason": None,
        "scenario_id": None,
        "created_at": "2026-08-22T08:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def valid_verdict(**overrides) -> dict:
    payload = {
        "signal_id": "3f1a1c8e-4a2b-4f31-9c1e-8b0f2a6d7e55",
        "osint_signal_id": "ext-99213",
        "verdict": "true_signal",
        "analyst_note": "ยืนยันจากแหล่งข่าวในพื้นที่",
        "closed_at": "2026-08-23T04:15:00+00:00",
    }
    payload.update(overrides)
    return payload


# ── outbound signal ──────────────────────────────────────────────────────────


def test_sample_signal_is_valid(signal_validator):
    signal_validator.validate(valid_signal())


def test_signal_accepts_a_null_event_time_and_scenario_id(signal_validator):
    signal_validator.validate(valid_signal(scenario_id=None))


@pytest.mark.parametrize(
    "overrides",
    [
        {"signal_type": "rumour"},  # outside the enum
        {"categories": ["หมวดที่ไม่มีจริง"]},  # outside the closed category list
        {"combined_score": 1.4},  # out of range
        {"unexpected_field": True},  # additionalProperties is false
    ],
)
def test_invalid_signals_are_rejected(signal_validator, overrides):
    with pytest.raises(ValidationError):
        signal_validator.validate(valid_signal(**overrides))


def test_missing_required_signal_field_is_rejected(signal_validator):
    payload = valid_signal()
    del payload["created_at"]
    with pytest.raises(ValidationError):
        signal_validator.validate(payload)


def test_top_events_are_capped_at_ten(signal_validator):
    event = valid_signal()["top_events"][0]
    with pytest.raises(ValidationError):
        signal_validator.validate(valid_signal(top_events=[event] * 11))


# ── inbound verdict ──────────────────────────────────────────────────────────


def test_sample_verdict_is_valid(verdict_validator):
    verdict_validator.validate(valid_verdict())


def test_verdict_note_may_be_null(verdict_validator):
    verdict_validator.validate(valid_verdict(analyst_note=None))


@pytest.mark.parametrize(
    "overrides",
    [{"verdict": "maybe"}, {"signal_id": "not-a-uuid-shaped-value"}, {"extra": 1}],
)
def test_invalid_verdicts_are_rejected(verdict_validator, overrides):
    with pytest.raises(ValidationError):
        verdict_validator.validate(valid_verdict(**overrides))
