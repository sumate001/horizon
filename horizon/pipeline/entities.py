"""Step 3.5 — turn extracted names into entities that have an identity.

The extractor emits `actors` as free text, so the same person arrives as
"อนุทิน ชาญวีรกูล", "อนุทิน ชาญวีรกูล (Anutin Charnvirakul)" and
"Anutin Charnvirakul (อนุทิน ชาญวีรกูล)" and counts as three actors. This module
collapses those into one entity with one id, so everything downstream —
clustering, weak signals, and OSINT//DESK's cross-case memory — can ask "have we
seen this person before?" and get a truthful answer.

Two stages, in this order for a reason measured on 2,660 real mentions:

  Rules   collapse spelling variants. 96% precise on 84 hand-labelled groups and
          30/30 on a random sample of the tail. Free, deterministic, and they
          hand the model clean groups instead of 2,204 loose strings.

  Model   decides what the rules structurally cannot, because the question is
          about the world rather than about characters: a country is not its
          football team, an organisation is not its director, and two ministries
          with the same Thai name are not the same ministry if one is
          Singapore's. 83/84 against the same labels, and the one disagreement
          was the model being stricter than the label.

The parenthetical is the crux. Thai news packs three different things in there
and only two of them are names:

  STRONG  a transliteration or an abbreviation — "(Anutin Charnvirakul)",
          "(กกต.)". Safe to merge on.
  WEAK    a nickname — "(เท้ง)", "(บิ๊กดุลย์)". Never merges, because nicknames
          collide across people. It stays out of `aliases` for that reason:
          `aliases` is the lookup key on ingest, so anything put there merges by
          definition. "รัฐบาลไทย (ครม.)" and "คณะรัฐมนตรี (ครม.)" fused on a weak
          form alone. The surface forms are kept on `event_entities` regardless,
          which is where a nickname is still readable.
  DROP    a role, a place, a legal form — "(รองนายกรัฐมนตรี)", "(สิงคโปร์)",
          "(มหาชน)". Not a name at all. Merging on these produced
          "เอกนิติ นิติทัณฑ์ประภาศ + ยศชนัน วงศ์สวัสดิ์" — two different deputy
          prime ministers fused into one person.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from ..llm.ollama import OllamaClient, OllamaError, get_ollama
from ..llm.prompts import entity_messages

log = logging.getLogger(__name__)

ENTITY_TYPES = ("person", "org", "place", "team", "generic", "unknown")

#: Below this the resolution is written but flagged for a human. Merging two
#: people wrongly is worse than not merging: it shows up as false history in a
#: case file, and nobody downstream can tell it was a guess.
REVIEW_THRESHOLD = 0.75

#: What a reviewer is being asked to judge, in Thai, written once so the pipeline
#: and the repair pass mean the same thing by it.
RISK_JOINED_ON_BRACKET = "ผูกกับตัวตนที่มีอยู่ผ่านวงเล็บ ไม่ใช่จากชื่อที่ตรงกัน"
RISK_BRACKET_MERGE = "รวมชื่อที่สะกดต่างกันโดยอาศัยวงเล็บเป็นตัวเชื่อม ไม่ใช่จากชื่อที่ตรงกัน"
RISK_UNRELATED_NAMES = "ชื่อที่ปรากฏในตัวตนนี้ไม่เชื่อมถึงกัน อาจเป็นคนละสิ่งที่ถูกรวมไว้ด้วยกัน"
RISK_TYPE_MISMATCH = "เป็นคนละประเภทกัน เช่น บุคคลกับองค์กร"
RISK_DIFFERENT_ITEMS = "ผูกกับรายการวิกิดาต้าคนละรายการ"

#: These two say we do not know *what* the entity is, not merely that a merge
#: looked thin. Asking Wikidata to identify one is guaranteed to get an answer
#: about only half of it: the entity holding both นันทพงศ์ สุวรรณรัตน์ and the
#: agency he works for was handed the agency's Q-number while typed a person.
UNIDENTIFIABLE_RISKS = (RISK_UNRELATED_NAMES, RISK_TYPE_MISMATCH)

#: Honorifics and ranks. Dropping them is safe — they qualify a person, they
#: never distinguish two people who share a name.
PREFIXES = (
    "พล.ต.อ.", "พล.ต.ท.", "พล.ต.ต.", "พล.ร.อ.", "พล.อ.ท.", "พล.อ.", "พล.ท.",
    "ร.ต.อ.", "ร.ต.ท.", "ร.ต.ต.", "พ.ต.อ.", "พ.ต.ท.", "ศ.ดร.", "รศ.ดร.",
    "ผศ.ดร.", "ว่าที่ร้อยตรี", "นางสาว", "น.ส.", "ดร.", "ศ.", "รศ.", "ผศ.",
    "นาย", "นาง", "Lt. Gen.", "Maj. Gen.", "Pol. Gen.", "Gen.", "Mr.", "Mrs.",
    "Ms.", "Dr.",
)

#: A parenthetical opening with one of these describes what someone does, not
#: what they are called. Roles are shared by design — that is the whole problem.
#:
#: The list is measured, not guessed: the additions below are every role form
#: that survived as an alias across 3,762 real actor strings. "ภราดร ปริศนานันท
#: กุล" was carrying "รัฐมนตรีประจำสำนักนายกรัฐมนตรี" as an alias, so the next
#: person appointed to that post would have merged into him.
ROLE_MARKERS = (
    "รมว", "รมช", "รมต", "รอง", "ผู้", "ผอ", "ผบ", "ผกก", "ผวจ", "อธิบดี",
    "ปลัด", "เลขาธิการ", "แม่ทัพ", "เสธ", "สส.", "ส.ส.", "สว.", "ส.ว.",
    "นายก", "นายอำเภอ", "ประธาน", "หัวหน้า", "โฆษก", "กรรมการ", "เจ้าของ",
    "เจ้าพนักงาน", "เจ้าอาวาส", "ทนาย", "แพทย์", "นักวิชาการ", "อดีต",
    "ว่าที่", "รักษาการ", "ที่ปรึกษา", "รัฐมนตรี", "เอกอัครราชทูต", "กำนัน",
    "อาจารย์", "ภรรยา", "สามี", "บิดา", "มารดา",
)

#: Not a name either, and it slipped past every rule because it is neither a
#: role nor a place: "(อายุ 50 ปี)", "(30 คน)", "(ไม่ต่ำกว่า 15 คน)".
_MEASURE = re.compile(r"^(?:[\d,\s]+(?:คน|ราย|ปี)|อายุ\s*[\d,]+|ไม่(?:ต่ำ|เกิน)กว่า)")

#: Not names: legal forms, pseudonym markers, and the roles a news story assigns
#: to unnamed people. Four different dead men were merged into one entity
#: because each was written "(ผู้เสียชีวิต)".
DROP_EXACT = frozenset({
    "มหาชน", "องค์การมหาชน", "นามสมมติ", "นามสมมุติ", "นามแฝง", "สงวนชื่อ",
    "ผู้เสียชีวิต", "ผู้ก่อเหตุ", "ผู้ต้องหา", "ผู้บาดเจ็บ", "เหยื่อ", "ผู้รอดชีวิต",
    "มือปืน",
})

#: A country in a bracket means one of two opposite things, and which one
#: decides whether we merge:
#:
#:   "กระทรวงการคลัง (สิงคโปร์)"      qualifies — it is what makes this ministry
#:                                    a different entity from the Thai one
#:   "United States (สหรัฐอเมริกา)"   translates — it is the same country twice
#:
#: Telling them apart needs both sides, so the forms are grouped by the place
#: they name rather than kept as a flat blocklist.
_PLACE_GROUPS = (
    ("ไทย", "ประเทศไทย", "thailand"),
    ("สิงคโปร์", "singapore"),
    ("ญี่ปุ่น", "japan"),
    ("เกาหลีใต้", "southkorea", "korea"),
    ("เกาหลีเหนือ", "northkorea"),
    ("จีน", "china"),
    ("สหรัฐ", "สหรัฐฯ", "สหรัฐอเมริกา", "unitedstates", "usa", "us", "america"),
    ("อังกฤษ", "สหราชอาณาจักร", "unitedkingdom", "uk", "britain", "england"),
    ("ฝรั่งเศส", "france"),
    ("เยอรมนี", "germany"),
    ("รัสเซีย", "russia"),
    ("อินเดีย", "india"),
    ("เวียดนาม", "vietnam"),
    ("กัมพูชา", "cambodia"),
    ("ลาว", "laos"),
    ("เมียนมา", "พม่า", "myanmar", "burma"),
    ("มาเลเซีย", "malaysia"),
    ("อินโดนีเซีย", "indonesia"),
    ("ฟิลิปปินส์", "philippines"),
    ("แคนาดา", "canada"),
    ("ยูเครน", "ukraine"),
    ("อิสราเอล", "israel"),
)
#: normalised surface form → the place it names
PLACE_FORMS: dict[str, str] = {
    norm_form: group[0] for group in _PLACE_GROUPS for norm_form in group
}

#: An English job title in a bracket looks exactly like a transliteration to the
#: script rule — "ประเสริฐ จันทรรวงทอง (Minister of Education)" is a role, not a
#: second name — so it has to be caught before that rule runs.
_ENGLISH_ROLE = re.compile(
    r"\b(minister|secretary|director|governor|president|chief|commander|deputy"
    r"|spokesman|spokesperson|chairman|head of|adviser|advisor|former)\b",
    re.IGNORECASE,
)

_PAREN = re.compile(r"[(（]([^)）]*)[)）]")
_SPACE = re.compile(r"[\s​]+")
_PUNCT = re.compile(r"[\"'`·,;:]")
_THAI = re.compile(r"[฀-๿]")
_LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class Mention:
    """One name as the extractor wrote it, with what we could make of it."""

    raw: str
    head: str
    strong: frozenset[str] = frozenset()
    weak: frozenset[str] = frozenset()
    qualifiers: frozenset[str] = frozenset()

    @property
    def keys(self) -> frozenset[str]:
        """Every form that may merge this mention with another."""
        return frozenset({self.head}) | self.strong


@dataclass
class Resolution:
    """What we decided a group of mentions is."""

    canonical: str
    entity_type: str
    mentions: list[str]
    confidence: float
    aliases: list[str] = field(default_factory=list)
    decided_by: str = "rules"

    #: Why this needs a human, if it does. Set from the shape of the merge
    #: rather than from the model's own estimate of itself.
    risk: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD or self.risk is not None

    @property
    def is_generic(self) -> bool:
        return self.entity_type == "generic"


def strip_prefix(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for prefix in PREFIXES:
            if name.startswith(prefix) and len(name) > len(prefix) + 1:
                name, changed = name[len(prefix):].strip(), True
    return name


def norm(name: str) -> str:
    """Comparison form: no honorific, no spaces, no punctuation, lower case."""
    name = unicodedata.normalize("NFC", name).strip()
    name = strip_prefix(name)
    return _PUNCT.sub("", _SPACE.sub("", name)).lower()


def is_abbrev_of(short: str, full: str) -> bool:
    """Thai abbreviates by keeping leading consonants — กกต. for คณะกรรมการการเลือกตั้ง.

    No prefix rule catches that, so this checks every character of the short form
    appears in the full form in order, and that the short form really is short.
    """
    core = re.sub(r"[.\s]", "", short)
    if not core or len(core) > len(full):
        return False
    if _LATIN.search(core) and core.isupper() and len(core) <= 8:
        return True  # DSI, ONWR, BMA — an acronym of the English rendering
    position = 0
    for char in core:
        position = full.find(char, position)
        if position < 0:
            return False
        position += 1
    return len(core) <= max(6, len(full) // 3)


def is_role(text: str) -> bool:
    """Does this describe what someone does rather than what they are called?"""
    bare = re.sub(r"^[\s.]+|[\s.]+$", "", text.strip())
    if len(bare) < 2:
        return False
    return any(bare.startswith(marker) for marker in ROLE_MARKERS) or bool(
        _ENGLISH_ROLE.search(bare)
    )


def _comma_parts(raw: str) -> list[str]:
    """Split on commas outside brackets, keeping the surrounding spacing."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(raw):
        if char in "(（":
            depth += 1
        elif char in ")）":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(raw[start:index])
            start = index + 1
    parts.append(raw[start:])
    return parts


def strip_role_clause(raw: str) -> str:
    """Drop a role appended after a comma: "อนุทิน ชาญวีรกูล, นายกรัฐมนตรี".

    The extractor writes the role in a bracket most of the time, and `classify`
    already refuses those. After a comma it survived into the head, and a head is
    what decides identity — so "อนุทิน ชาญวีรกูล, นายกรัฐมนตรี" became a second
    person, separate from "อนุทิน ชาญวีรกูล" and without his Wikidata id.

    The clause also carries its own bracket, and that was the more damaging half:
    "ฉัตรชัย บางชวด (Chatchai Bangchuad), เลขาธิการสภาความมั่นคงแห่งชาติ (สมช.)"
    made "สมช." a strong alias of a man, so every later mention of the National
    Security Council merged into him. Stripping the clause before the brackets
    are read is what stops that.

    Only a *role* is stripped, never every dropped kind. A country after a comma
    is the distinction that keeps Singapore's finance ministry apart from
    Thailand's, and removing it would reintroduce the merge the place rule exists
    to prevent. Commas that hold a legal form ("Co., Ltd."), part of a name
    ("กระทรวงวัฒนธรรม, กีฬา และการท่องเที่ยว…") or a number ("54,000 คน") are not
    roles and stay.
    """
    parts = _comma_parts(raw)
    while len(parts) > 1 and is_role(parts[-1]):
        parts.pop()
    return ",".join(parts).strip()


def classify(inner: str, head: str) -> str:
    """STRONG (a name), WEAK (a nickname) or DROP (not a name)."""
    bare = re.sub(r"^[\s.]+|[\s.]+$", "", inner.strip())
    if len(bare) < 2:
        return "DROP"
    if bare in DROP_EXACT:
        return "DROP"
    if is_role(bare) or _MEASURE.match(bare):
        return "DROP"
    place = PLACE_FORMS.get(norm(bare))
    if place is not None:
        # Same place on both sides means it is a translation, not a qualifier.
        return "STRONG" if PLACE_FORMS.get(norm(head)) == place else "DROP"
    if is_abbrev_of(bare, head):
        return "STRONG"
    # Script crossing is the transliteration signal: a Thai head with a Latin
    # bracket, or the reverse, is one name written twice.
    head_thai, head_latin = bool(_THAI.search(head)), bool(_LATIN.search(head))
    bare_thai, bare_latin = bool(_THAI.search(bare)), bool(_LATIN.search(bare))
    if head_thai and bare_latin and not bare_thai:
        return "STRONG"
    if head_latin and bare_thai and not bare_latin:
        return "STRONG"
    return "WEAK"


def _head_and_brackets(raw: str) -> tuple[str, list[str]]:
    """The name proper and the brackets that belong to it, role clause removed."""
    body = strip_role_clause(raw)
    return _SPACE.sub(" ", _PAREN.sub(" ", body)).strip(), _PAREN.findall(body)


def parse(raw: str) -> Mention:
    """Split a raw actor string into a head and its classified parentheticals."""
    head_text, inner = _head_and_brackets(raw)
    strong, weak, qualifiers = set(), set(), set()
    for part in inner:
        kind = classify(part, head_text)
        target = {"STRONG": strong, "WEAK": weak}.get(kind, qualifiers)
        cleaned = norm(part)
        if len(cleaned) >= 2:
            target.add(cleaned)
    return Mention(
        raw=raw,
        head=norm(head_text),
        strong=frozenset(strong),
        weak=frozenset(weak),
        qualifiers=frozenset(qualifiers),
    )


def group_by_rules(raws: list[str], *, respect_qualifiers: bool = True) -> list[list[str]]:
    """Collapse spelling variants. Union-find over shared head or strong alias.

    Mentions whose heads match but whose dropped qualifiers differ are kept
    apart: "กระทรวงการคลัง" and "กระทรวงการคลัง (สิงคโปร์)" share a head, and the
    qualifier is the only thing telling two ministries apart.

    `respect_qualifiers=False` asks the weaker question "do these names touch at
    all?", which is what the repair pass needs: an entity whose surface forms
    fall into two components is holding two different things and wants a human,
    while one that splits only on a qualifier is merely a ministry with a country
    attached and is fine.
    """
    mentions = {raw: parse(raw) for raw in dict.fromkeys(raws)}
    parent: dict[str, str] = {raw: raw for raw in mentions}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[a] = b

    by_key: dict[str, list[str]] = {}
    for raw, mention in mentions.items():
        for key in mention.keys:
            by_key.setdefault(key, []).append(raw)

    for members in by_key.values():
        first = members[0]
        for other in members[1:]:
            if respect_qualifiers and mentions[first].qualifiers != mentions[other].qualifiers:
                continue  # a qualifier is a distinction, not a spelling
            union(first, other)

    groups: dict[str, list[str]] = {}
    for raw in mentions:
        groups.setdefault(find(raw), []).append(raw)
    return [sorted(members) for members in groups.values()]


def _longest(members: list[str]) -> str:
    """Fall back to the fullest spelling — it carries the most information."""
    return max(members, key=lambda raw: (len(parse(raw).head), raw))


def display_name(members: list[str], proposed: str | None = None) -> str:
    """The name an analyst should see, decided here rather than by the model.

    Left to itself the model answers inconsistently — "เลพาส (LEPAS)" with the
    bracket still attached for one entity and "Chal Wang" for the next, where
    the article said "นายชาล หวัง". This is a Thai newsroom, so: no brackets, no
    honorific, and the Thai form wins when the story used one.

    What the article called the thing outranks anything in a bracket, and the
    bracket is consulted only when no head offers a name in the winning script —
    "Chal Wang (นายชาล หวัง)". Ranking the two together renamed people after
    whatever was in the bracket, because "longest Thai" has no way of preferring
    a person to their job title:

        "ฐนัตถ์ สุวรรณานนท์ (ผู้อำนวยการสำนักข่าวกรองแห่งชาติ)"  →  the title
        "ป้าเกล็น (เหมืองสมศักดิ์)"                              →  the mine

    A bracket that `classify` reads as a role or a qualifier is not a name at
    all and never becomes one.
    """
    def clean(text: str) -> str:
        body = _PAREN.sub("", strip_role_clause(text)).strip()
        return _SPACE.sub(" ", strip_prefix(body)).strip()

    heads = [clean(raw) for raw in members]
    if proposed:
        heads.append(clean(proposed))
    brackets: list[str] = []
    for raw in members:
        head_text, parts = _head_and_brackets(raw)
        brackets += [clean(part) for part in parts if classify(part, head_text) == "STRONG"]

    def thai(names: list[str]) -> list[str]:
        return [name for name in names if name and _THAI.search(name)]

    heads = [name for name in heads if name]
    brackets = [name for name in brackets if name]
    pool = thai(heads) or thai(brackets) or heads or brackets
    if not pool:
        return (proposed or members[0]).strip()
    # Longest within the winning script: "คณะกรรมการการเลือกตั้ง" over "กกต.".
    return max(pool, key=len)


def merge_is_risky(members: list[str]) -> str | None:
    """Why a human should look at this merge, or None if the shape is safe.

    Self-reported confidence turned out to be useless as a trigger: over a 40
    event backfill the model returned 1.0 for 141 of 142 entities, so a queue
    gated on it stays empty whether or not the merges are right.

    So the gate is the shape of the evidence instead: did the merge rest on a
    shared spelling, or on a bracket?

      "อนุทิน ชาญวีรกูล"  +  "อนุทิน ชาญวีรกูล (Anutin Charnvirakul)"
          same head once the honorific and bracket are stripped — safe

      "iLaw"  +  "ยิ่งชีพ อัชฌานนท์ (iLaw)"
          different heads, joined only because one appears in the other's
          bracket — this is the shape every wrong merge had

    This deliberately flags merges that are obviously right, including
    "DSI" + "กรมสอบสวนคดีพิเศษ (DSI)". That pair is *structurally identical* to
    the iLaw one — a short Latin name matching the bracket of a long Thai name —
    and the only thing separating them is knowing that DSI is what the
    department is called while iLaw is where the man works. No string rule
    reaches that, so the choice is to queue both or to trust both, and trusting
    both is how false history gets into a case file. The queue stays small: over
    the 40 event backfill, 9 of 142 entities merged more than one surface form.
    """
    if len({parse(raw).head for raw in members}) < 2:
        return None
    return RISK_BRACKET_MERGE


def rule_resolution(members: list[str]) -> Resolution:
    """What the rules alone would conclude. Used when the model is unavailable."""
    mention = parse(_longest(members))
    aliases = sorted({m for raw in members for m in parse(raw).keys})
    return Resolution(
        canonical=display_name(members),
        entity_type="unknown",
        mentions=sorted(members),
        # Deliberately below REVIEW_THRESHOLD: rules never assign a type, and an
        # untyped entity should not enter the store as though it were settled.
        confidence=0.5,
        aliases=aliases or [mention.head],
        decided_by="rules",
        risk=merge_is_risky(members),
    )


def _apply(members: list[str], payload: dict) -> list[Resolution]:
    """Turn the model's answer into resolutions, keeping every mention exactly once."""
    out: list[Resolution] = []
    claimed: set[str] = set()
    for group in payload.get("groups", []):
        picked = [
            members[i]
            for i in group.get("members", [])
            if isinstance(i, int) and 0 <= i < len(members) and members[i] not in claimed
        ]
        if not picked:
            continue
        claimed.update(picked)
        entity_type = group.get("type")
        confidence = group.get("confidence")
        out.append(
            Resolution(
                canonical=display_name(picked, group.get("canonical")),
                entity_type=entity_type if entity_type in ENTITY_TYPES else "unknown",
                mentions=sorted(picked),
                # A missing confidence means the model did not commit, so neither
                # do we — it goes to the queue rather than in as fact.
                confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.5,
                aliases=sorted({a for raw in picked for a in parse(raw).keys}),
                decided_by="llm",
                risk=merge_is_risky(picked),
            )
        )

    missed = [raw for raw in members if raw not in claimed]
    if missed:
        # The model dropped names. Keeping them as an unreviewed group is wrong
        # in a quiet way, so they go to a human instead.
        log.warning("entity adjudication dropped mentions", extra={"missed": missed})
        fallback = rule_resolution(missed)
        fallback.confidence = 0.0
        out.append(fallback)
    return out


async def adjudicate(
    members: list[str],
    *,
    context: str | None = None,
    client: OllamaClient | None = None,
    model: str | None = None,
) -> list[Resolution]:
    """Ask the model whether these names are one thing, and what kind of thing.

    Falls back to the rules on any LLM failure — an entity nobody typed is worth
    more than an article that never finished ingesting.
    """
    if len(members) == 1 and not context:
        pass  # still worth asking: a lone "ตำรวจ" should be typed generic
    client = client or get_ollama()
    try:
        payload = await client.chat_json(
            entity_messages(members, context=context), purpose="entities", model=model
        )
    except (OllamaError, ValueError) as exc:
        log.warning("entity adjudication failed, falling back to rules", extra={"error": str(exc)})
        return [rule_resolution(members)]
    return _apply(members, payload) or [rule_resolution(members)]


async def resolve(
    raws: list[str],
    *,
    context: str | None = None,
    client: OllamaClient | None = None,
    model: str | None = None,
) -> list[Resolution]:
    """Full pass: rules narrow the field, the model decides, generics are dropped.

    Generic nouns are returned rather than silently discarded — the caller logs
    what it refused so "ตำรวจ appeared 400 times and none of it is an entity" is
    visible instead of being a gap.
    """
    resolutions: list[Resolution] = []
    for group in group_by_rules([raw for raw in raws if raw and raw.strip()]):
        resolutions.extend(await adjudicate(group, context=context, client=client, model=model))
    return resolutions


# ── persistence ──────────────────────────────────────────────────────────────


async def _find_existing(session, aliases: list[str]):
    """An entity already answering to one of these surface forms.

    Array overlap against the GIN index, so this stays a lookup rather than a
    scan as the store grows. Rejected entities are excluded: an analyst has
    already said this grouping was wrong, and matching it again would undo them.
    """
    from sqlalchemy import select

    from ..models import Entity

    if not aliases:
        return None
    rows = (
        await session.execute(
            select(Entity)
            .where(Entity.aliases.overlap(aliases))
            .where(Entity.review_status != "rejected")
            .order_by(Entity.mention_count.desc())
            .limit(1)
        )
    ).scalars()
    return next(iter(rows), None)


async def persist(session, event_id, resolutions: list[Resolution]) -> dict[str, int]:
    """Write resolutions as entities and link them to the event.

    Generic nouns are counted and skipped. "ตำรวจ" is not a thing anyone can
    investigate, and letting it into the store would make it the most connected
    node in the graph purely by being a common word.
    """
    import uuid as _uuid
    from datetime import UTC, datetime

    from ..models import Entity, EventEntity

    # Key names avoid "created", "message", "module" and friends: these dicts are
    # splatted into logging `extra`, and LogRecord raises on a reserved name.
    counts = {"entities_linked": 0, "entities_new": 0, "generic_skipped": 0, "needs_review": 0}
    for resolution in resolutions:
        if resolution.is_generic:
            counts["generic_skipped"] += 1
            continue

        entity = await _find_existing(session, resolution.aliases)
        # The risky merge is almost never inside one article — an article rarely
        # spells the same name two ways. It happens here, when a new mention is
        # attached to an entity seen days ago. If the two agree on a head form
        # this is the same name written twice; if they only meet through a
        # bracket, it is the shape that produced "iLaw" + its director.
        joined_on_bracket = entity is not None and not (
            {key for raw in resolution.mentions for key in {parse(raw).head}}
            & set(entity.aliases)
        )
        if entity is None:
            entity = Entity(
                id=_uuid.uuid4(),
                canonical_name=resolution.canonical,
                entity_type=resolution.entity_type,
                aliases=resolution.aliases,
                confidence=resolution.confidence,
                decided_by=resolution.decided_by,
                review_status="needs_review" if resolution.needs_review else "auto",
                # Set here rather than left to the column default: that default
                # only lands at INSERT, and this counter is incremented below.
                mention_count=0,
                risk=resolution.risk,
            )
            session.add(entity)
            await session.flush()
            counts["entities_new"] += 1
        else:
            # Widen what the entity answers to, and let a confident typing fill
            # in one that an earlier pass left unknown.
            merged = sorted(set(entity.aliases) | set(resolution.aliases))
            if merged != entity.aliases:
                entity.aliases = merged
            if entity.entity_type == "unknown" and resolution.entity_type != "unknown":
                entity.entity_type = resolution.entity_type
            entity.confidence = max(entity.confidence, resolution.confidence)
            counts["entities_linked"] += 1

        entity.mention_count += len(resolution.mentions)
        entity.last_seen = datetime.now(UTC)
        risk = resolution.risk or (
            RISK_JOINED_ON_BRACKET if joined_on_bracket else None
        )
        if risk and entity.review_status == "auto":
            entity.review_status = "needs_review"
            entity.risk = entity.risk or risk
        if entity.review_status == "needs_review":
            counts["needs_review"] += 1

        for surface in resolution.mentions:
            session.add(
                EventEntity(
                    id=_uuid.uuid4(),
                    event_id=event_id,
                    entity_id=entity.id,
                    surface_form=surface,
                )
            )
    return counts
