"""The repair pass, judged on the decisions it makes rather than on the writes.

`run()` needs a database, so what is tested here is everything that decides:
what a stored entity should be called, and whether two of them may be folded
together. Both bad merges in the first dry run over 1,396 real entities are kept
as cases, because a repair that quietly makes the store worse is the one failure
this command cannot be allowed to have.
"""

import pytest

from horizon.models import Entity
from horizon.pipeline.entities import (
    RISK_JOINED_ON_BRACKET,
    RISK_UNRELATED_NAMES,
    group_by_rules,
)
from horizon.pipeline.repair_entities import _derive, _flag, _may_merge


def _entity(**kwargs) -> Entity:
    kwargs.setdefault("entity_type", "unknown")
    kwargs.setdefault("review_status", "auto")
    return Entity(canonical_name="x", aliases=[], **kwargs)


# ── what a stored entity should have been called ─────────────────────────────


def test_a_role_clause_no_longer_makes_a_second_prime_minister():
    name, aliases = _derive(["อนุทิน ชาญวีรกูล (Anutin Charnvirook), นายกรัฐมนตรี"])

    assert name == "อนุทิน ชาญวีรกูล"
    assert "อนุทินชาญวีรกูล" in aliases
    assert "อนุทินชาญวีรกูลนายกรัฐมนตรี" not in aliases


def test_the_agency_in_a_role_clause_is_no_longer_an_alias_of_the_person():
    _, aliases = _derive(
        ["ฉัตรชัย บางชวด (Chatchai Bangchuad), เลขาธิการสภาความมั่นคงแห่งชาติ (สมช.)"]
    )

    assert aliases == ["chatchaibangchuad", "ฉัตรชัยบางชวด"]


def test_a_person_stored_under_their_job_title_gets_their_name_back():
    name, _ = _derive(
        [
            "ฐนัตถ์ สุวรรณานนท์ (Thanut Suwannanon)",
            "ฐนัตถ์ สุวรรณานนท์ (ผู้อำนวยการสำนักข่าวกรองแห่งชาติ)",
        ]
    )

    assert name == "ฐนัตถ์ สุวรรณานนท์"


# ── which entities may be folded together ────────────────────────────────────


def test_a_person_is_never_folded_into_an_organisation():
    """The dry run tried to merge a member of parliament into his party, and the
    director of iLaw into iLaw, both because a bracket carried the other's name.
    No amount of shared spelling makes a man his employer."""
    refusal = _may_merge(_entity(entity_type="org"), _entity(entity_type="person"))

    assert refusal is not None


def test_two_different_wikidata_items_are_never_folded_together():
    refusal = _may_merge(
        _entity(entity_type="person", qid="Q1"), _entity(entity_type="person", qid="Q2")
    )

    assert refusal is not None


@pytest.mark.parametrize(
    "left,right",
    [
        ({"entity_type": "org"}, {"entity_type": "org"}),
        ({"entity_type": "person", "qid": "Q1"}, {"entity_type": "person"}),
        ({"entity_type": "unknown"}, {"entity_type": "person"}),
        ({"entity_type": "person", "qid": "Q1"}, {"entity_type": "person", "qid": "Q1"}),
    ],
)
def test_agreeing_entities_may_merge(left, right):
    assert _may_merge(_entity(**left), _entity(**right)) is None


# ── which entities need a human instead ──────────────────────────────────────


def test_an_entity_holding_a_man_and_his_agency_is_detected():
    """This is what the repair refuses to decide. นันทพงศ์ สุวรรณรัตน์ had
    absorbed ศอ.บต. entirely, and only someone who can read the articles can say
    which mentions belong to which."""
    forms = [
        "นันทพงศ์ สุวรรณรัตน์ (Nantapong Suwannarat)",
        "ศอ.บต.",
        "ศูนย์อำนวยการบริหารจังหวัดชายแดนภาคใต้ (ศอ.บต.)",
    ]

    assert len(group_by_rules(forms, respect_qualifiers=False)) > 1


def test_one_person_spelled_several_ways_is_not_mistaken_for_two_things():
    """The same test must stay quiet on an entity that is merely well covered."""
    forms = [
        "ฉัตรชัย บางชวด (Chatchai Bangchuad)",
        "ฉัตรชัย บางชวด (เลขาธิการสภาความมั่นคงแห่งชาติ)",
        "ฉัตรชัย บางชวด (Chatchai Bangchuad), เลขาธิการสภาความมั่นคงแห่งชาติ (สมช.)",
    ]

    assert len(group_by_rules(forms, respect_qualifiers=False)) == 1


def test_flagging_something_unidentifiable_takes_back_its_wikidata_id():
    """A Q-number on an entity that holds two things is worse than none.

    นันทพงศ์ สุวรรณรัตน์ and the agency he works for shared one row; the linker
    read the name, which by then was the agency's, and stored the agency's item
    on a row typed `person`. Half right, and indistinguishable from settled.
    """
    entity = _entity(entity_type="person", qid="Q13021506", qid_status="linked")

    _flag(entity, RISK_UNRELATED_NAMES, apply=True)

    assert entity.qid is None
    assert entity.qid_status == "pending"
    assert entity.review_status == "needs_review"


def test_a_specific_reason_replaces_the_one_written_at_ingest():
    """"joined on a bracket" says look; "holds two things" says what to decide."""
    entity = _entity(review_status="needs_review", risk=RISK_JOINED_ON_BRACKET)

    _flag(entity, RISK_UNRELATED_NAMES, apply=True)

    assert entity.risk == RISK_UNRELATED_NAMES


def test_reporting_never_writes():
    entity = _entity(qid="Q1", qid_status="linked")

    _flag(entity, RISK_UNRELATED_NAMES, apply=False)

    assert entity.qid == "Q1"
    assert entity.review_status == "auto"
