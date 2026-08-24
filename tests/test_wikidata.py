"""Wikidata linking, tested against the failures actually observed.

Every case below is something the API really returned for a name really present
in this corpus. Nothing here hits the network: the point is the decision logic,
and the measured evidence is pinned as fixtures so it stays reproducible.
"""

import types

import pytest

from horizon.pipeline.wikidata import (
    NO_MATCH,
    Candidate,
    Link,
    WikidataClient,
    _plausible,
    _retry_after,
    choose,
    link_entity,
)

# ── real search results, copied from live responses ──────────────────────────

#: What Wikidata returns for "แคนาดา". Q16, the country, is absent — its Thai
#: label is "ประเทศแคนาดา" and the bare form is not an alias.
CANADA_HITS = [
    Candidate("Q2704874", "แคนาดา เน็กซ์ ท็อป โมเดล", "Canadian reality show", "prefix"),
    Candidate("Q85377", "กลุ่มเกาะอาร์กติกแคนาดา", "archipelago in northern North America", "fulltext"),
    Candidate("Q1104069", "ดอลลาร์แคนาดา", "currency of Canada", "fulltext"),
]

#: "กระทรวงการคลัง" — five foreign ministries rank above the Thai one.
TREASURY_HITS = [
    Candidate(
        "Q578269", "กระทรวงการคลัง", "economic and finance ministry of the United Kingdom", "prefix"
    ),
    Candidate("Q832407", "กระทรวงการคลัง", "กระทรวงในรัฐบาลจีน", "prefix"),
    Candidate("Q1416522", "กระทรวงการคลัง (ประเทศไทย)", "กระทรวงรัฐบาลไทย", "fulltext"),
]


class _Stub:
    """An Ollama stand-in that returns one canned answer."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def chat_json(self, messages, **_kwargs):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload




# ── the filter that runs before the model ────────────────────────────────────


@pytest.mark.parametrize(
    "description",
    [
        "Canadian reality show",
        "1984 debut studio album by Aed Carabao",
        "Wikimedia disambiguation page",
    ],
)
@pytest.mark.asyncio
async def test_a_work_is_not_the_thing_it_is_named_after(description):
    """A song called "กัมพูชา" is never the country in a border story."""
    assert not _plausible(Candidate("Q1", "x", description, "prefix"), "place")


@pytest.mark.asyncio
async def test_a_real_item_survives_the_filter():
    cambodia = Candidate("Q424", "ประเทศกัมพูชา", "ราชอาณาจักรในเอเชียตะวันออกเฉียงใต้", "fulltext")
    assert _plausible(cambodia, "place")


@pytest.mark.asyncio
async def test_filtering_everything_out_means_no_match_without_asking_the_model():
    stub = _Stub({"qid": "Q2704874", "confidence": 1.0})
    link = await choose("แคนาดา", [CANADA_HITS[0]], entity_type="place", client=stub)
    assert not link.linked and stub.calls == 0


# ── choosing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_right_item_is_picked_from_a_list_of_near_misses():
    stub = _Stub({"qid": "Q1416522", "confidence": 0.95, "reason": "บริบทเป็นข่าวไทย"})
    link = await choose(
        "กระทรวงการคลัง",
        TREASURY_HITS,
        entity_type="org",
        context="รมว.คลังแถลงมาตรการของไทย",
        client=stub,
    )
    assert link.qid == "Q1416522" and link.confidence == 0.95


@pytest.mark.asyncio
async def test_none_is_an_allowed_answer():
    """The correct item is genuinely absent about a fifth of the time."""
    stub = _Stub({"qid": None, "confidence": 0.9, "reason": "ไม่มีประเทศแคนาดาในตัวเลือก"})
    link = await choose("แคนาดา", CANADA_HITS, entity_type="place", client=stub)
    assert not link.linked and "แคนาดา" in link.reason


@pytest.mark.parametrize("answer", ["none", "None", "-1", ""])
@pytest.mark.asyncio
async def test_the_various_ways_a_model_says_no_are_all_understood(answer):
    stub = _Stub({"qid": answer, "confidence": 0.5})
    assert not (await choose("x", TREASURY_HITS, client=stub)).linked


@pytest.mark.asyncio
async def test_a_qid_that_was_not_offered_is_refused():
    """Accepting it would link to an item nothing in this run ever checked."""
    stub = _Stub({"qid": "Q99999999", "confidence": 1.0})
    link = await choose("กระทรวงการคลัง", TREASURY_HITS, entity_type="org", client=stub)
    assert not link.linked and link.confidence == 0.0


@pytest.mark.asyncio
async def test_a_model_failure_is_not_a_match():
    stub = _Stub(ValueError("bad json"))
    link = await choose("x", TREASURY_HITS, client=stub)
    assert not link.linked and "เลือกไม่สำเร็จ" in link.reason


@pytest.mark.asyncio
async def test_no_candidates_at_all_needs_no_model_call():
    stub = _Stub({"qid": "Q1", "confidence": 1.0})
    link = await choose("ผู้นำชุมชนบ้านทุ่งแพม", [], entity_type="person", client=stub)
    assert link.reason == NO_MATCH and stub.calls == 0


# ── writing the outcome onto an entity ───────────────────────────────────────


def _entity(**overrides):
    base = {
        "canonical_name": "กระทรวงการคลัง",
        "entity_type": "org",
        "aliases": ["กระทรวงการคลัง"],
        "qid": None,
        "qid_status": "pending",
        "qid_confidence": None,
        "qid_reason": None,
        "qid_checked_at": None,
        "review_status": "auto",
        "risk": None,
    }
    return types.SimpleNamespace(**{**base, **overrides})


class _FakeWikidata:
    def __init__(self, hits):
        self.hits = hits

    async def candidates(self, name, *, also=None, language="th"):
        return self.hits


@pytest.mark.asyncio
async def test_a_confident_link_is_stored_and_not_queued():
    entity = _entity()
    await link_entity(
        entity,
        wikidata=_FakeWikidata(TREASURY_HITS),
        client=_Stub({"qid": "Q1416522", "confidence": 0.95}),
    )
    assert entity.qid == "Q1416522"
    assert entity.qid_status == "linked"
    assert entity.review_status == "auto"


@pytest.mark.asyncio
async def test_a_shaky_link_is_stored_but_sent_to_a_human():
    """A wrong Q-number looks authoritative everywhere it travels."""
    entity = _entity()
    await link_entity(
        entity,
        wikidata=_FakeWikidata(TREASURY_HITS),
        client=_Stub({"qid": "Q1416522", "confidence": 0.5}),
    )
    assert entity.qid == "Q1416522"
    assert entity.review_status == "needs_review"
    assert "Q1416522" in entity.risk


@pytest.mark.asyncio
async def test_no_match_is_recorded_so_the_name_is_never_re_asked():
    entity = _entity(canonical_name="ผู้นำชุมชนบ้านทุ่งแพม", entity_type="person")
    await link_entity(entity, wikidata=_FakeWikidata([]), client=_Stub({}))
    assert entity.qid_status == "no_match"
    assert entity.qid_checked_at is not None


@pytest.mark.asyncio
async def test_a_network_failure_stays_retryable():
    """An outage must not permanently mark half the store as unlinkable."""

    class _Down:
        async def candidates(self, name, *, also=None, language="th"):
            from horizon.pipeline.wikidata import WikidataUnavailable

            raise WikidataUnavailable("connection refused")

    entity = _entity()
    await link_entity(entity, wikidata=_Down(), client=_Stub({}))
    assert entity.qid_status == "unavailable"
    assert entity.qid is None


@pytest.mark.asyncio
async def test_a_generic_noun_is_never_linked():
    """Wikidata has an item for "police"; the article was about some officers."""
    entity = _entity(canonical_name="ตำรวจ", entity_type="generic")
    stub = _Stub({"qid": "Q35535", "confidence": 1.0})
    await link_entity(entity, wikidata=_FakeWikidata([]), client=stub)
    assert entity.qid is None and entity.qid_status == "no_match" and stub.calls == 0


# ── being a polite API client ────────────────────────────────────────────────
# Synchronous, so the module-level asyncio mark does not apply here.


def test_retry_after_is_read_when_the_server_sends_it():
    response = types.SimpleNamespace(headers={"retry-after": "12"})
    assert _retry_after(response) == 12.0


def test_a_date_form_retry_after_falls_back_to_our_own_backoff():
    response = types.SimpleNamespace(headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _retry_after(response) is None


def test_the_user_agent_identifies_us():
    """Wikimedia throttles anonymous clients harder, and may block them."""
    assert "horizon" in WikidataClient().user_agent.lower()


def test_a_link_knows_whether_it_linked():
    assert Link("Q1", 1.0, "").linked
    assert not Link(None, 0.0, NO_MATCH).linked
