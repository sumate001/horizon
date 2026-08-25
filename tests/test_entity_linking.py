"""Deciding whether a new mention is someone the store already knows.

This is the step that produced every wrong merge in the system, and until now no
model was involved in it: an alias index returned the most-mentioned entity
sharing a character sequence, and that entity was merged into. Character overlap
cannot tell a person from the agency they run, so the fix is not a better index
— it is to let the index suggest and the model decide.

What is tested here is the decision, given candidates as plain data. The
protocol matters as much as the judgement: an answer outside the offered list,
or no answer at all, must not become a merge.
"""

import uuid

import pytest

from horizon.llm.ollama import OllamaError
from horizon.models import Entity
from horizon.pipeline.entities import (
    RISK_LINK_UNDECIDED,
    RISK_UNSURE_LINK,
    UNIDENTIFIABLE_RISKS,
    Resolution,
    _candidates,
    choose_existing,
    parse,
    persist,
)

pytestmark = pytest.mark.asyncio


class _StubClient:
    """An Ollama client that answers with whatever the test hands it."""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload, self.error = payload, error
        self.messages = None

    async def chat_json(self, messages, *, purpose="chat", model=None):
        self.messages = messages
        if self.error:
            raise self.error
        return self.payload


def _resolution(canonical: str, mentions: list[str], kind: str = "person") -> Resolution:
    # Real aliases, because an empty list short-circuits the candidate lookup and
    # would make these tests pass without a decision ever being made.
    return Resolution(
        canonical=canonical,
        entity_type=kind,
        mentions=mentions,
        confidence=1.0,
        aliases=sorted({key for raw in mentions for key in parse(raw).keys}),
        decided_by="llm",
    )


#: The two entities the index keeps offering each other, because "iLaw" appears
#: in the man's bracket. Kept as data so both directions can be asked.
ILAW = ("iLaw", "org", ["iLaw", "ไอลอว์ (iLaw)"])
ANUTIN = ("อนุทิน ชาญวีรกูล", "person", ["อนุทิน ชาญวีรกูล", "Anutin Charnvirakul"])


# ── the protocol ─────────────────────────────────────────────────────────────


async def test_no_candidates_means_no_question_is_asked():
    """The common case by far, and it must not cost a model call."""
    client = _StubClient(payload={"match": 0})

    picked, _, _ = await choose_existing(_resolution("ใครสักคน", ["ใครสักคน"]), [], client=client)

    assert picked is None
    assert client.messages is None


async def test_a_model_that_cannot_be_reached_does_not_merge():
    """The reason this is not a close call: a duplicate is repairable and a
    wrong merge is not. Whatever the index suggested stays a suggestion."""
    client = _StubClient(error=OllamaError("ollama is down"))

    picked, _, reason = await choose_existing(
        _resolution("ยิ่งชีพ อัชฌานนท์", ["ยิ่งชีพ อัชฌานนท์ (iLaw)"]), [ILAW], client=client
    )

    assert picked is None
    assert reason == RISK_LINK_UNDECIDED


async def test_a_candidate_that_was_never_offered_is_refused():
    """Honouring an out-of-range index would merge into whatever sits there."""
    client = _StubClient(payload={"match": 7, "confidence": 1.0, "reason": "มั่นใจมาก"})

    picked, _, _ = await choose_existing(
        _resolution("ยิ่งชีพ อัชฌานนท์", ["ยิ่งชีพ อัชฌานนท์"]), [ILAW], client=client
    )

    assert picked is None


async def test_null_is_a_real_answer_not_a_missing_one():
    client = _StubClient(
        payload={"match": None, "confidence": 0.9, "reason": "iLaw คือองค์กร ไม่ใช่ตัวบุคคล"}
    )

    picked, confidence, reason = await choose_existing(
        _resolution("ยิ่งชีพ อัชฌานนท์", ["ยิ่งชีพ อัชฌานนท์ (iLaw)"]), [ILAW], client=client
    )

    assert picked is None
    assert confidence == 0.9
    assert "องค์กร" in reason


async def test_a_missing_confidence_is_not_read_as_certainty():
    """Written and queued, rather than written as though it were settled."""
    client = _StubClient(payload={"match": 0, "reason": "น่าจะใช่"})

    _, confidence, _ = await choose_existing(
        _resolution("อนุทิน ชาญวีรกูล", ["อนุทิน ชาญวีรกูล"]), [ANUTIN], client=client
    )

    assert confidence == 0.5


async def test_a_match_is_returned_with_the_model_reasoning():
    client = _StubClient(
        payload={"match": 0, "confidence": 0.95, "reason": "ชื่อเดียวกัน คนละการสะกด"}
    )

    picked, confidence, reason = await choose_existing(
        _resolution("Anutin Charnvirakul", ["Anutin Charnvirakul"]), [ANUTIN], client=client
    )

    assert picked == 0
    assert confidence == 0.95
    assert reason == "ชื่อเดียวกัน คนละการสะกด"


# ── what the model is actually shown ─────────────────────────────────────────


async def test_the_article_and_the_recorded_spellings_both_reach_the_model():
    """The two things the old string comparison could not see.

    An entity's canonical name can be actively misleading — one named "ยิ่งชีพ
    อัชฌานนท์" whose recorded spellings are "iLaw" and "ไอลอว์" is an
    organisation wearing a person's name — so the spellings go in as evidence,
    and the article says which one the story is about.
    """
    client = _StubClient(payload={"match": None, "confidence": 1.0, "reason": "ไม่ใช่"})

    await choose_existing(
        _resolution("ยิ่งชีพ อัชฌานนท์", ["ยิ่งชีพ อัชฌานนท์ (iLaw)"]),
        [ILAW],
        context="ผู้อำนวยการ iLaw แถลงคัดค้านร่างกฎหมาย",
        client=client,
    )

    sent = "\n".join(message["content"] for message in client.messages)
    assert "ผู้อำนวยการ iLaw แถลงคัดค้านร่างกฎหมาย" in sent
    assert "ไอลอว์ (iLaw)" in sent
    assert "ยิ่งชีพ อัชฌานนท์ (iLaw)" in sent


async def test_the_prompt_says_that_none_is_allowed():
    """A prompt reading "pick the best" turns a broad search into a broad merge,
    and the search is deliberately broad now."""
    client = _StubClient(payload={"match": None, "confidence": 1.0, "reason": "ไม่ใช่"})

    await choose_existing(_resolution("ก", ["ก"]), [ILAW], client=client)

    system = client.messages[0]["content"]
    assert "null" in system
    assert "ไม่รวมดีกว่ารวมผิด" in system


# ── the decision as `persist` acts on it ─────────────────────────────────────


class _StubSession:
    """A session that offers a fixed set of candidates and records writes."""

    def __init__(self, candidates: list | None = None) -> None:
        self.candidates = candidates or []
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, *_args, **_kwargs):
        rows = self.candidates

        class _Result:
            def scalars(self):
                return iter(rows)

            def all(self):
                return []  # surface forms are looked up separately; none stubbed

        return _Result()


async def test_an_entity_known_to_hold_two_things_is_never_offered():
    """Withheld in the query, because there is no right answer to give about it.

    Asked whether a mention of นันทพงศ์ สุวรรณรัตน์ was the entity that had
    swallowed ศอ.บต., the model said yes — reasonably, since that row really does
    answer to both. A corrupt candidate can only spread, so it is kept out of the
    search rather than argued with in the prompt.
    """

    class _Capture:
        def __init__(self) -> None:
            self.statement = None

        async def execute(self, statement, *_args, **_kwargs):
            self.statement = statement

            class _Result:
                def scalars(self):
                    return iter(())

            return _Result()

    session = _Capture()
    await _candidates(session, ["อนุทินชาญวีรกูล"])

    compiled = str(session.statement.compile(compile_kwargs={"literal_binds": True}))
    for risk in UNIDENTIFIABLE_RISKS:
        assert risk in compiled
    assert "rejected" in compiled


def _stored(name: str, kind: str, aliases: list[str]) -> Entity:
    return Entity(
        id=uuid.uuid4(),
        canonical_name=name,
        entity_type=kind,
        aliases=aliases,
        confidence=1.0,
        review_status="auto",
        mention_count=9,
    )


async def test_persist_does_not_merge_when_the_model_says_no():
    """The whole point, stated as the outcome an analyst would see.

    "ยิ่งชีพ อัชฌานนท์ (iLaw)" overlaps the iLaw entity on characters, so the
    index offers it — as it always did. What changed is that the offer is now
    answerable, and answering "no" leaves two entities instead of a man merged
    into his employer.
    """
    stored = _stored("iLaw", "org", ["ilaw", "ไอลอว์"])
    session = _StubSession([stored])
    client = _StubClient(payload={"match": None, "confidence": 1.0, "reason": "คนละสิ่ง"})

    counts = await persist(
        session,
        uuid.uuid4(),
        [_resolution("ยิ่งชีพ อัชฌานนท์", ["ยิ่งชีพ อัชฌานนท์ (iLaw)"])],
        context="ผู้อำนวยการ iLaw แถลง",
        client=client,
    )

    assert counts["entities_new"] == 1
    assert counts["entities_linked"] == 0
    assert stored.mention_count == 9


async def test_persist_merges_when_the_model_says_yes():
    stored = _stored("อนุทิน ชาญวีรกูล", "person", ["อนุทินชาญวีรกูล"])
    session = _StubSession([stored])
    client = _StubClient(payload={"match": 0, "confidence": 1.0, "reason": "คนเดียวกัน"})

    counts = await persist(
        session,
        uuid.uuid4(),
        [_resolution("Anutin Charnvirakul", ["Anutin Charnvirakul"])],
        client=client,
    )

    assert counts["entities_linked"] == 1
    assert counts["entities_new"] == 0
    assert stored.mention_count == 10
    assert stored.review_status == "auto"


async def test_a_merge_the_model_was_unsure_of_is_written_and_queued():
    stored = _stored("อนุทิน ชาญวีรกูล", "person", ["อนุทินชาญวีรกูล"])
    session = _StubSession([stored])
    client = _StubClient(payload={"match": 0, "confidence": 0.4, "reason": "ไม่ค่อยแน่ใจ"})

    counts = await persist(
        session, uuid.uuid4(), [_resolution("อนุทิน", ["อนุทิน"])], client=client
    )

    assert counts["entities_linked"] == 1
    assert stored.review_status == "needs_review"
    assert stored.risk == RISK_UNSURE_LINK


async def test_an_unreachable_model_leaves_a_duplicate_that_says_so():
    """Not silently: the duplicate is queued, because a store that quietly grows
    two rows for one person is the failure this module exists to prevent."""
    stored = _stored("อนุทิน ชาญวีรกูล", "person", ["อนุทินชาญวีรกูล"])
    session = _StubSession([stored])
    client = _StubClient(error=OllamaError("ollama is down"))

    counts = await persist(
        session, uuid.uuid4(), [_resolution("อนุทิน ชาญวีรกูล", ["อนุทิน ชาญวีรกูล"])], client=client
    )

    assert counts["entities_new"] == 1
    assert stored.mention_count == 9
    created = next(obj for obj in session.added if isinstance(obj, Entity))
    assert created.review_status == "needs_review"
    assert created.risk == RISK_LINK_UNDECIDED
