"""Outbound delivery and the inbound verdict contract."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from horizon.config import get_settings
from horizon.integration.osint_desk import (
    BACKOFF_SECONDS,
    INBOUND_PATH,
    DeliveryOutcome,
    is_retryable,
    next_attempt,
    post_signal,
)
from horizon.services.api import VERDICT_TO_STATUS, VerdictIn

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
BASE_URL = "http://osintdesk.test"
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def enabled(monkeypatch):
    """Point the client at a fake OSINT//DESK."""
    get_settings.cache_clear()
    monkeypatch.setenv("OSINT_DESK_BASE_URL", BASE_URL)
    monkeypatch.setenv("OSINT_DESK_API_KEY", "secret-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def payload() -> dict:
    return {
        "signal_id": "3f1a1c8e-4a2b-4f31-9c1e-8b0f2a6d7e55",
        "signal_type": "weak_signal",
        "title": "สัญญาณอ่อน",
        "combined_score": 0.72,
        "trend_score": 0.0,
        "categories": ["พลังงาน"],
        "summary": "สรุป",
        "top_events": [],
        "force_assessments": [],
        "scenario_id": None,
        "created_at": NOW.isoformat(),
    }


# ── backoff schedule ─────────────────────────────────────────────────────────


def test_the_backoff_matches_the_spec():
    assert BACKOFF_SECONDS == (30, 120, 600, 3600)


@pytest.mark.parametrize(
    "attempts,delay",
    [(1, 30), (2, 120), (3, 600), (4, 3600)],
)
def test_each_failure_pushes_the_next_attempt_further_out(attempts, delay):
    assert next_attempt(attempts, now=NOW) == NOW + timedelta(seconds=delay)


def test_the_schedule_runs_out_after_the_last_step():
    """Four failures exhaust the list; the fifth has nowhere to go but `failed`."""
    assert next_attempt(len(BACKOFF_SECONDS) + 1, now=NOW) is None


def test_a_nonsense_attempt_count_does_not_schedule_anything():
    assert next_attempt(0, now=NOW) is None
    assert next_attempt(-3, now=NOW) is None


def test_the_whole_schedule_spans_a_little_over_an_hour():
    """Long enough that in-memory retry state would not survive a restart —
    which is why it lives on the dispatches row."""
    assert sum(BACKOFF_SECONDS) == 4350


# ── which failures are worth retrying ────────────────────────────────────────


@pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 425, 429])
def test_server_trouble_and_rate_limits_are_retried(code):
    assert is_retryable(code) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
def test_a_rejected_payload_is_not_retried(code):
    """Resending an unchanged body to a 4xx just repeats the rejection."""
    assert is_retryable(code) is False


# ── the HTTP call ────────────────────────────────────────────────────────────


async def test_with_no_base_url_configured_delivery_is_disabled_not_failed(monkeypatch):
    """Clears the variable explicitly rather than assuming the ambient .env is
    empty — it stopped being empty the moment the integration was wired up."""
    monkeypatch.setenv("OSINT_DESK_BASE_URL", "")
    get_settings.cache_clear()
    outcome = await post_signal(payload())
    assert outcome == DeliveryOutcome(status="disabled")
    get_settings.cache_clear()


@respx.mock
async def test_a_202_records_the_returned_signal_id(enabled):
    route = respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(
        return_value=httpx.Response(202, json={"osint_signal_id": "ext-4471"})
    )
    outcome = await post_signal(payload())
    assert outcome.status == "delivered"
    assert outcome.osint_signal_id == "ext-4471"
    assert route.called


@respx.mock
async def test_the_api_key_travels_with_the_request(enabled):
    route = respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(
        return_value=httpx.Response(202, json={"osint_signal_id": "ext-1"})
    )
    await post_signal(payload())
    assert route.calls.last.request.headers["X-API-Key"] == "secret-key"
    assert route.calls.last.request.headers["Content-Type"] == "application/json"


@respx.mock
async def test_the_posted_body_is_the_payload_verbatim(enabled):
    route = respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(
        return_value=httpx.Response(202, json={"osint_signal_id": "ext-1"})
    )
    await post_signal(payload())
    assert json.loads(route.calls.last.request.content) == payload()


@respx.mock
async def test_a_202_without_an_id_still_counts_as_delivered(enabled):
    """Accepted is accepted; the missing id costs traceability, not delivery."""
    respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(return_value=httpx.Response(202, json={}))
    outcome = await post_signal(payload())
    assert outcome.status == "delivered"
    assert outcome.osint_signal_id is None


@respx.mock
async def test_a_server_error_comes_back_as_pending(enabled):
    respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(return_value=httpx.Response(503, text="down"))
    outcome = await post_signal(payload())
    assert outcome.status == "pending"
    assert "503" in outcome.error


@respx.mock
async def test_a_rejection_fails_immediately_without_burning_retries(enabled):
    respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(
        return_value=httpx.Response(422, text="bad payload")
    )
    outcome = await post_signal(payload())
    assert outcome.status == "failed"


@respx.mock
async def test_a_connection_error_is_retryable_and_never_raises(enabled):
    """OSINT//DESK being unreachable must not surface as an exception — the
    pipeline keeps running whatever the other system is doing."""
    respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(side_effect=httpx.ConnectError("refused"))
    outcome = await post_signal(payload())
    assert outcome.status == "pending"
    assert "transport" in outcome.error


@respx.mock
async def test_a_timeout_is_retryable(enabled):
    respx.post(f"{BASE_URL}{INBOUND_PATH}").mock(side_effect=httpx.ReadTimeout("slow"))
    outcome = await post_signal(payload())
    assert outcome.status == "pending"


# ── inbound verdict contract ─────────────────────────────────────────────────


def verdict_body(**overrides) -> dict:
    body = {
        "signal_id": "3f1a1c8e-4a2b-4f31-9c1e-8b0f2a6d7e55",
        "osint_signal_id": "ext-99213",
        "verdict": "true_signal",
        "analyst_note": "ยืนยันจากแหล่งข่าวในพื้นที่",
        "closed_at": "2026-08-23T04:15:00+00:00",
    }
    body.update(overrides)
    return body


def test_the_inbound_model_accepts_the_contract_body():
    parsed = VerdictIn(**verdict_body())
    assert parsed.verdict == "true_signal"
    assert parsed.osint_signal_id == "ext-99213"


def test_the_inbound_model_matches_the_shared_schema_field_for_field():
    """Pydantic and the JSON Schema must agree, or one repo drifts silently."""
    schema = json.loads((CONTRACTS / "verdict.schema.json").read_text(encoding="utf-8"))
    assert set(VerdictIn.model_fields) == set(schema["required"])
    assert set(VerdictIn.model_fields) == set(schema["properties"])


def test_the_same_body_satisfies_both_the_schema_and_the_model():
    schema = json.loads((CONTRACTS / "verdict.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    body = verdict_body()
    validator.validate(body)
    VerdictIn(**body)


def test_a_note_may_be_omitted():
    parsed = VerdictIn(**{k: v for k, v in verdict_body().items() if k != "analyst_note"})
    assert parsed.analyst_note is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"verdict": "maybe"},
        {"signal_id": "not-a-uuid"},
        {"closed_at": "เมื่อวานนี้"},
        {"unexpected": True},
    ],
)
def test_a_malformed_verdict_is_rejected_by_the_model(overrides):
    with pytest.raises(ValidationError):
        VerdictIn(**verdict_body(**overrides))


# ── verdict → weak signal status ─────────────────────────────────────────────


def test_a_decided_verdict_moves_the_weak_signal_on():
    assert VERDICT_TO_STATUS["true_signal"] == "verified_true"
    assert VERDICT_TO_STATUS["false_signal"] == "verified_false"


def test_inconclusive_leaves_the_weak_signal_where_it_was():
    """An analyst who could not tell has not told us anything to learn from."""
    assert "inconclusive" not in VERDICT_TO_STATUS
