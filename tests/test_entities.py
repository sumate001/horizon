"""Entity resolution, measured against hand labels rather than asserted by taste.

Every case here came out of 2,660 real actor mentions from the running system.
The merges that appear as `SAME` were checked by eye; the ones as `DIFFERENT`
are merges the string rules actually made and got wrong, which is why each has
its own test rather than sitting in a list.

The precision test at the bottom is the point of the file: it fails if a change
to the rules drops accuracy on the labelled set, so tuning a regex shows up as a
number instead of as a surprise three weeks later.
"""

import uuid

import pytest

from horizon.pipeline.entities import (
    REVIEW_THRESHOLD,
    Resolution,
    _apply,
    classify,
    group_by_rules,
    is_abbrev_of,
    norm,
    parse,
    rule_resolution,
)


class _StubSession:
    """Just enough session for persist(): no database, no lookups, no I/O."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, *_args, **_kwargs):
        class _Empty:
            def scalars(self):
                return iter(())

        return _Empty()


# ── the labelled set ─────────────────────────────────────────────────────────

#: Spellings of one thing. Rules are expected to collapse each of these.
SAME = [
    [
        "อนุทิน ชาญวีรกูล",
        "อนุทิน ชาญวีรกูล (Anutin Charnvirakul)",
        "Anutin Charnvirakul (อนุทิน ชาญวีรกูล)",
    ],
    ["คณะกรรมการการเลือกตั้ง (กกต.)", "กกต. (ECT)", "คณะกรรมการการเลือกตั้ง (ECT)"],
    ["กรมสอบสวนคดีพิเศษ (DSI)", "DSI", "DSI (ดีเอสไอ)", "ดีเอสไอ (DSI)"],
    ["โดนัลด์ ทรัมป์ (Donald Trump)", "Donald Trump (โดนัลด์ ทรัมป์)"],
    ["พรรคประชาธิปัตย์", "พรรคประชาธิปัตย์ (Democrat Party)", "พรรคประชาธิปัตย์ (ปชป.)"],
    ["อาร์เซนอล (Arsenal)", "อาร์เซน่อล (Arsenal)"],
    ["สหรัฐอเมริกา (USA)", "สหรัฐฯ (USA)", "United States (สหรัฐอเมริกา)"],
    ["กรุงเทพมหานคร (กทม.)", "กรุงเทพมหานคร (BMA)", "Bangkok Metropolitan Administration (BMA)"],
    ["มหาวิทยาลัยเชียงใหม่ (มช.)", "มหาวิทยาลัยเชียงใหม่ (Chiang Mai University)"],
    ["จีน", "จีน (China)"],
]

#: Merges the rules made on real data that were wrong. Rules alone cannot fix
#: the first two — the strings genuinely look alike — so those are marked xfail
#: against the rule layer and are the reason the model stage exists.
DIFFERENT = [
    pytest.param(
        ["กระทรวงการคลัง", "กระทรวงการคลัง (สิงคโปร์)"],
        id="ministries-in-different-countries",
    ),
    pytest.param(
        ["เอกนิติ นิติทัณฑ์ประภาศ (รองนายกรัฐมนตรี)", "ยศชนัน วงศ์สวัสดิ์ (รองนายกรัฐมนตรี)"],
        id="two-people-sharing-a-title",
    ),
    pytest.param(
        ["นายวิชัย กัญญา (ผู้เสียชีวิต)", "นายสมพงษ์ (ผู้เสียชีวิต)"],
        id="two-people-sharing-a-fate",
    ),
    pytest.param(
        ["กระทรวงการคลัง (สิงคโปร์)", "กระทรวงแรงงาน (สิงคโปร์)"],
        id="two-ministries-sharing-a-country",
    ),
]


# ── normalisation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("นายอนุทิน ชาญวีรกูล", "อนุทินชาญวีรกูล"),
        ("พล.ท.อดุลย์ บุญธรรมเจริญ", "อดุลย์บุญธรรมเจริญ"),
        ("ศ.ดร.ยศชนัน วงศ์สวัสดิ์", "ยศชนันวงศ์สวัสดิ์"),
        ("Dr. Ekniti Nitithanprapas", "ekniti nitithanprapas".replace(" ", "")),
    ],
)
def test_honorifics_do_not_make_a_different_person(raw, expected):
    assert norm(raw) == expected


def test_a_bare_honorific_is_left_alone():
    """Stripping must not eat the whole name and leave an empty key."""
    assert norm("นาย") == "นาย"


@pytest.mark.parametrize(
    "short,full",
    [
        ("กกต.", "คณะกรรมการการเลือกตั้ง"),
        ("ปภ.", "กรมป้องกันและบรรเทาสาธารณภัย"),
        ("DSI", "กรมสอบสวนคดีพิเศษ"),
    ],
)
def test_thai_abbreviations_are_recognised(short, full):
    assert is_abbrev_of(short, full)


def test_a_different_agency_is_not_an_abbreviation():
    assert not is_abbrev_of("กระทรวงแรงงาน", "กระทรวงการคลัง")


# ── what belongs in a bracket ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "inner,head",
    [
        ("Anutin Charnvirakul", "อนุทิน ชาญวีรกูล"),
        ("กกต.", "คณะกรรมการการเลือกตั้ง"),
        ("อนุทิน ชาญวีรกูล", "Anutin Charnvirakul"),
    ],
)
def test_a_transliteration_or_abbreviation_is_a_name(inner, head):
    assert classify(inner, head) == "STRONG"


@pytest.mark.parametrize(
    "inner",
    ["รองนายกรัฐมนตรี", "รมว.มหาดไทย", "ผู้เสียชีวิต", "สิงคโปร์", "มหาชน", "อธิบดีกรมการปกครอง"],
)
def test_a_role_or_qualifier_is_not_a_name(inner):
    """Roles are shared by design, which is exactly what makes them dangerous."""
    assert classify(inner, "someone") == "DROP"


def test_a_country_beside_the_same_country_is_a_translation():
    assert classify("สหรัฐอเมริกา", "United States") == "STRONG"
    assert classify("Canada", "แคนาดา") == "STRONG"


def test_a_country_beside_something_else_is_a_qualifier():
    """The same bracket, the opposite meaning — it is what separates two ministries."""
    assert classify("สิงคโปร์", "กระทรวงการคลัง") == "DROP"
    assert classify("ไทย", "กระทรวงศึกษาธิการ") == "DROP"


def test_an_english_job_title_is_not_a_transliteration():
    """Script crossing alone would read this as a second name."""
    assert classify("Minister of Education", "ประเสริฐ จันทรรวงทอง") == "DROP"


@pytest.mark.parametrize("inner", ["เท้ง", "บิ๊กดุลย์", "ช้างศึก"])
def test_a_nickname_is_recorded_but_never_merges_alone(inner):
    assert classify(inner, "ชื่อจริงคนหนึ่ง") == "WEAK"
    assert norm(inner) not in parse(f"ชื่อจริงคนหนึ่ง ({inner})").keys


# ── grouping ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("members", SAME, ids=[m[0][:24] for m in SAME])
def test_spellings_of_one_thing_collapse(members):
    assert len(group_by_rules(members)) == 1


@pytest.mark.parametrize("members", DIFFERENT)
def test_different_things_are_kept_apart(members):
    groups = group_by_rules(members)
    assert len(groups) == len(members), f"merged: {groups}"


def test_grouping_never_loses_or_duplicates_a_mention():
    everything = [name for group in SAME for name in group]
    grouped = [name for group in group_by_rules(everything) for name in group]
    assert sorted(grouped) == sorted(set(everything))


def test_a_group_is_named_by_its_fullest_spelling():
    resolution = rule_resolution(["กกต. (ECT)", "คณะกรรมการการเลือกตั้ง (กกต.)"])
    assert resolution.canonical == "คณะกรรมการการเลือกตั้ง"


def test_rules_alone_never_claim_confidence():
    """No type, no confidence: an untyped entity must not enter as settled fact."""
    resolution = rule_resolution(["อนุทิน ชาญวีรกูล"])
    assert resolution.entity_type == "unknown"
    assert resolution.needs_review


# ── reading the model's answer ───────────────────────────────────────────────


def test_a_split_answer_becomes_two_resolutions():
    members = ["ไทย (Thailand)", "ทีมชาติไทย", "ทีมชาติไทย (Thailand)"]
    payload = {
        "groups": [
            {"canonical": "ประเทศไทย", "type": "place", "members": [0], "confidence": 1.0},
            {"canonical": "ทีมชาติไทย", "type": "team", "members": [1, 2], "confidence": 1.0},
        ]
    }
    place, team = sorted(_apply(members, payload), key=lambda r: r.entity_type)
    assert place.entity_type == "place" and place.mentions == ["ไทย (Thailand)"]
    assert team.entity_type == "team" and len(team.mentions) == 2


def test_a_dropped_mention_goes_to_a_human_not_into_the_store():
    """Silence is the failure mode that matters: a name that vanishes is invisible."""
    members = ["iLaw", "ยิ่งชีพ อัชฌานนท์ (iLaw)"]
    payload = {"groups": [{"canonical": "iLaw", "type": "org", "members": [0], "confidence": 1.0}]}
    resolutions = _apply(members, payload)
    assert sorted(m for r in resolutions for m in r.mentions) == sorted(members)
    leftover = next(r for r in resolutions if "ยิ่งชีพ อัชฌานนท์ (iLaw)" in r.mentions)
    assert leftover.needs_review


def test_a_mention_claimed_twice_is_only_counted_once():
    members = ["ก", "ข"]
    payload = {
        "groups": [
            {"canonical": "ก", "type": "person", "members": [0, 1], "confidence": 0.9},
            {"canonical": "ข", "type": "person", "members": [1], "confidence": 0.9},
        ]
    }
    resolutions = _apply(members, payload)
    assert sorted(m for r in resolutions for m in r.mentions) == ["ก", "ข"]


def test_a_missing_confidence_is_treated_as_uncertain():
    payload = {"groups": [{"canonical": "ก", "type": "person", "members": [0]}]}
    assert _apply(["ก"], payload)[0].needs_review


@pytest.mark.parametrize("value,expected", [(0.9, False), (REVIEW_THRESHOLD, False), (0.5, True)])
def test_the_review_threshold_is_the_line(value, expected):
    assert Resolution("ก", "person", ["ก"], value).needs_review is expected


def test_a_generic_noun_is_typed_and_kept_out():
    payload = {
        "groups": [{"canonical": "ตำรวจ", "type": "generic", "members": [0], "confidence": 1.0}]
    }
    assert _apply(["ตำรวจ"], payload)[0].is_generic


def test_an_unknown_type_from_the_model_is_not_trusted_into_the_schema():
    payload = {"groups": [{"canonical": "ก", "type": "สิ่งของ", "members": [0], "confidence": 1.0}]}
    assert _apply(["ก"], payload)[0].entity_type == "unknown"


# ── the number that must not quietly fall ────────────────────────────────────


def test_rule_precision_on_the_labelled_set():
    """Measured, not asserted: 14/14 groups when this was written.

    SAME must collapse, DIFFERENT must not. Two DIFFERENT cases are decidable by
    characters (a shared role, a shared country); the other two are not, and the
    rules pass them only because a qualifier survives as a distinction.
    """
    correct = sum(len(group_by_rules(members)) == 1 for members in SAME)
    correct += sum(
        len(group_by_rules(case.values[0])) == len(case.values[0]) for case in DIFFERENT
    )
    total = len(SAME) + len(DIFFERENT)
    assert correct == total, f"rule precision fell to {correct}/{total}"


# ── the name an analyst reads ────────────────────────────────────────────────


def test_the_display_name_drops_brackets_and_honorifics():
    from horizon.pipeline.entities import display_name

    assert display_name(["นายจิตติศักดิ์ วงษ์ศิริ (Jittisak Wongsirip)"]) == "จิตติศักดิ์ วงษ์ศิริ"


def test_the_thai_form_wins_when_the_story_used_one():
    """The model answered "Chal Wang" for an article that said "นายชาล หวัง"."""
    from horizon.pipeline.entities import display_name

    assert display_name(["นายชาล หวัง (Chal Wang)"], "Chal Wang") == "ชาล หวัง"


def test_the_fullest_thai_form_wins_over_its_abbreviation():
    from horizon.pipeline.entities import display_name

    assert display_name(["กกต. (ECT)", "คณะกรรมการการเลือกตั้ง (กกต.)"]) == "คณะกรรมการการเลือกตั้ง"


def test_an_english_only_entity_keeps_its_english_name():
    from horizon.pipeline.entities import display_name

    assert display_name(["GISTDA"]) == "GISTDA"


async def test_the_counters_can_be_splatted_into_a_log_record():
    """`persist` counts go straight into logging `extra` in the worker.

    LogRecord raises on a reserved name, and because the worker logs outside its
    try block, a counter called "created" failed the whole article rather than
    just the log line. This runs the real counters through a real logger.
    """
    import logging

    from horizon.pipeline.entities import persist

    counts = await persist(_StubSession(), uuid.uuid4(), [
        Resolution("อนุทิน ชาญวีรกูล", "person", ["อนุทิน ชาญวีรกูล"], 1.0, ["อนุทินชาญวีรกูล"]),
        Resolution("ตำรวจ", "generic", ["ตำรวจ"], 1.0, ["ตำรวจ"]),
    ])

    assert counts["entities_new"] == 1 and counts["generic_skipped"] == 1
    logging.getLogger("test").makeRecord("test", 20, "p", 1, "m", None, None, extra=counts)


# ── what actually gets a human's attention ───────────────────────────────────


@pytest.mark.parametrize(
    "members",
    [
        ["iLaw", "ยิ่งชีพ อัชฌานนท์ (iLaw)"],
        ["ไทย (Thailand)", "ทีมชาติไทย (Thailand)"],
    ],
    ids=["org-and-its-director", "country-and-its-team"],
)
def test_a_merge_across_unrelated_names_is_flagged(members):
    """The shape shared by every wrong merge found in the evaluation."""
    from horizon.pipeline.entities import merge_is_risky

    assert merge_is_risky(members) is not None


@pytest.mark.parametrize(
    "members",
    [
        ["พล.ท.อดุลย์ บุญธรรมเจริญ", "อดุลย์ บุญธรรมเจริญ (Adul Boonthamcharoen)"],
        ["อนุทิน ชาญวีรกูล", "อนุทิน ชาญวีรกูล (Anutin Charnvirakul)"],
        ["จีน", "จีน (China)"],
    ],
    ids=["rank-stripped", "same-name-transliterated", "thai-plus-english"],
)
def test_a_merge_on_a_shared_spelling_is_not_flagged(members):
    """Same head once the honorific and bracket come off — nothing to judge."""
    from horizon.pipeline.entities import merge_is_risky

    assert merge_is_risky(members) is None


def test_an_obviously_correct_cross_name_merge_is_flagged_too():
    """DSI + กรมสอบสวนคดีพิเศษ is right, and still queued.

    It has the same shape as the iLaw merge that was wrong — a short Latin name
    matching the bracket of a long Thai one. Nothing cheap tells them apart, so
    both go to a human rather than both being trusted.
    """
    from horizon.pipeline.entities import merge_is_risky

    assert merge_is_risky(["DSI", "กรมสอบสวนคดีพิเศษ (DSI)"]) is not None


def test_a_confident_model_does_not_override_a_risky_shape():
    """141 of 142 entities came back at confidence 1.0, so confidence alone is no gate."""
    members = ["iLaw", "ยิ่งชีพ อัชฌานนท์ (iLaw)"]
    payload = {
        "groups": [{"canonical": "iLaw", "type": "org", "members": [0, 1], "confidence": 1.0}]
    }
    assert _apply(members, payload)[0].needs_review
