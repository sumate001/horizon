"""Step 3.5 — turn extracted names into entities that have an identity.

The extractor emits `actors` as free text, so the same person arrives as
"อนุทิน ชาญวีรกูล", "อนุทิน ชาญวีรกูล (Anutin Charnvirakul)" and
"Anutin Charnvirakul (อนุทิน ชาญวีรกูล)" and counts as three actors. This module
collapses those into one entity with one id, so everything downstream —
clustering, weak signals, and OSINT//DESK's cross-case memory — can ask "have we
seen this person before?" and get a truthful answer.

**Rules search, the model decides.** That division is the whole design, and it
was arrived at the hard way: every wrong merge this system produced came from a
step where a string comparison was allowed to decide, and every rule added to
prevent one was a patch on that mistake rather than a fix for it.

It also says where the risk is not. Three of the four steps below are cheap or
near-trivial on real data; the one that decides whether today's mention is
someone the store already knows is where every wrong merge has come from, and it
is the only one carrying an uncertainty check.

  `group_by_rules`   which mentions in *this article* are worth showing the
                     model together. Cheap, deterministic, generous — and in
                     practice it almost never has anything to do: measured over
                     the whole store it produced 5,324 groups of which exactly
                     one held more than a single mention. An article writes each
                     name one way. The spelling variants this module exists for
                     appear *between* articles, not inside one.

  `adjudicate`       the model says which of them are the same thing and what
                     kind of thing. Given the above, its real job on almost every
                     call is typing a lone name — person, org, or a generic noun
                     like "ตำรวจ" that must be kept out of the store entirely.

  `_candidates`      which of ~1,400 stored entities *might* be this one. A
                     search, not an answer — the store will not fit in a prompt,
                     so something cheap has to shortlist, and this is it.

  `choose_existing`  the model reads the article and the candidates' recorded
                     spellings, and says which one it is, or none. This is the
                     merge that matters: "the same person as last Tuesday" is a
                     claim about the world, and characters cannot check it.

Trusting that answer needs a measure of doubt, and the model's own number is not
one: `confidence` comes back exactly 1.0 in 98% of answers on this corpus, and
on the Wikidata step it only ever takes two values — 1.0 when the answer is yes
and 0 when it is no. It restates the answer rather than measuring it. So three
signals are gathered instead, none of which asks the model to introspect:

  agreement   the same question with the two sides exchanged. "Is the same thing
              as" is symmetric, so an answer that flips was never held firmly.
  counter     the model must write the strongest case *against* before it
              decides — first in the JSON, because generation is left to right.
  hedging     words in `reason` — its decision sentence — that mark it
              overriding evidence it just acknowledged. Both wrong merges in
              testing were carried by this: "แม้จะเป็นตำแหน่ง แต่…" at a
              self-reported 0.90, and "แม้ในข่าวจะระบุว่าเป็นของสิงคโปร์" at 1.00.
              `counter` is excluded from the scan on purpose — doubt is that
              field's whole job.

The parenthetical still gets classified, but only to build search keys, and the
cost of being wrong is now a candidate the model declines rather than a merge
nobody sees:

  STRONG  a transliteration or an abbreviation — "(Anutin Charnvirakul)", "(กกต.)"
  WEAK    a nickname — "(เท้ง)", "(บิ๊กดุลย์)". Kept out of `aliases`, because
          nicknames collide across people and a shortlist built from them is
          mostly noise. Still readable on `event_entities.surface_form`.
  DROP    a role, a place, a legal form — "(รองนายกรัฐมนตรี)", "(สิงคโปร์)",
          "(มหาชน)". Not a name at all, so not a search key either.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace

from ..llm.ollama import OllamaClient, OllamaError, get_ollama
from ..llm.prompts import entity_link_messages, entity_messages

log = logging.getLogger(__name__)

ENTITY_TYPES = ("person", "org", "place", "team", "generic", "unknown")

#: Below this the resolution is written but flagged for a human. Merging two
#: people wrongly is worse than not merging: it shows up as false history in a
#: case file, and nobody downstream can tell it was a guess.
REVIEW_THRESHOLD = 0.75

#: What a reviewer is being asked to judge, in Thai, written once so the pipeline
#: and the repair pass mean the same thing by it.
#:
#: The first is no longer written by anything: it was the string rule's guess at
#: whether a cross-event merge was safe, and `choose_existing` asks the model
#: now. Kept because rows in the store still carry it and a reviewer still has to
#: read them.
RISK_JOINED_ON_BRACKET = "ผูกกับตัวตนที่มีอยู่ผ่านวงเล็บ ไม่ใช่จากชื่อที่ตรงกัน"
RISK_BRACKET_MERGE = "รวมชื่อที่สะกดต่างกันโดยอาศัยวงเล็บเป็นตัวเชื่อม ไม่ใช่จากชื่อที่ตรงกัน"
RISK_UNSURE_LINK = "รวมกับตัวตนเดิมโดยที่โมเดลยังไม่มั่นใจ"
RISK_LINK_CONTRADICTED = "ไม่รวมกับตัวตนเดิม เพราะโมเดลตอบขัดกันเอง อาจเป็นตัวตนซ้ำ"
RISK_LINK_UNDECIDED = "ยังไม่ได้ตัดสินว่าซ้ำกับตัวตนเดิมหรือไม่ เพราะโมเดลไม่ตอบ"
RISK_NAMES_DROPPED = "โมเดลไม่ได้จัดชื่อเหล่านี้เข้ากลุ่มใดเลย ยังไม่มีใครตัดสินว่ามันคืออะไร"
RISK_UNRELATED_NAMES = "ชื่อที่ปรากฏในตัวตนนี้ไม่เชื่อมถึงกัน อาจเป็นคนละสิ่งที่ถูกรวมไว้ด้วยกัน"
RISK_TYPE_MISMATCH = "เป็นคนละประเภทกัน เช่น บุคคลกับองค์กร"
RISK_DIFFERENT_ITEMS = "ผูกกับรายการวิกิดาต้าคนละรายการ"
RISK_SHARED_QID = "ชี้ไปที่รายการวิกิดาต้าเดียวกับตัวตนอื่น น่าจะเป็นสิ่งเดียวกัน"
#: needs_review has two causes — a risk, or a confidence below the threshold —
#: and only the first one used to write down why. The second put 75 entities in
#: the queue carrying nothing but a name, which an analyst cannot act on and
#: cannot dismiss either. A flag without a reason is indistinguishable from
#: noise, and a queue that is mostly noise stops being read.
RISK_LOW_CONFIDENCE = "ยังไม่มั่นใจว่าชื่อนี้คือสิ่งใด"

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
    def review_risk(self) -> str | None:
        """The reason to show in the queue — never None while needs_review."""
        if self.risk is not None:
            return self.risk
        if self.confidence < REVIEW_THRESHOLD:
            return f"{RISK_LOW_CONFIDENCE} (ความมั่นใจ {self.confidence:.2f})"
        return None

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
        # Say so in the queue. These reached a human already, but with an empty
        # reason column — which tells a reviewer that something is wrong without
        # telling them what, and nine of them were sitting there like that.
        fallback.risk = RISK_NAMES_DROPPED
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


#: How many stored entities to put in front of the model. The index is allowed
#: to be generous now that it no longer decides, but a list this long is already
#: past the point where more candidates buy anything: on this corpus a mention
#: overlaps at most a handful of entities, and the rest of the array is noise the
#: model has to read past.
MAX_CANDIDATES = 5


async def _candidates(session, aliases: list[str], limit: int = MAX_CANDIDATES):
    """Entities that *might* be this one — a search, not an answer.

    Array overlap against the GIN index, so this stays a lookup rather than a
    scan as the store grows.

    Two kinds of entity are withheld. Rejected ones, because an analyst has
    already said the grouping was wrong and matching it again would undo them.
    And ones flagged as holding two different things, because there is no right
    answer to give about those: asked whether a mention of นันทพงศ์ สุวรรณรัตน์
    was the entity that had swallowed ศอ.บต., the model said yes — reasonably,
    since the row really does answer to both. Offering a corrupt row as a
    candidate can only spread it, so the mention starts a clean entity and the
    corrupt one waits for the human it is already queued for.

    This used to return one row and that row was merged into, which made the
    index the decision-maker. Character overlap cannot tell a person from the
    agency they run, so every wrong merge this system produced came from here —
    and every rule written to prevent one was a patch on a search being asked to
    also be a judgement.
    """
    from sqlalchemy import or_, select

    from ..models import Entity

    if not aliases:
        return []
    return list(
        (
            await session.execute(
                select(Entity)
                .where(Entity.aliases.overlap(aliases))
                .where(Entity.review_status != "rejected")
                # NULL is the common case and `NOT IN` against it is NULL, which
                # would withhold every healthy entity instead of the sick ones.
                .where(
                    or_(Entity.risk.is_(None), Entity.risk.notin_(UNIDENTIFIABLE_RISKS))
                )
                .order_by(Entity.mention_count.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def _surface_forms_of(session, entity_ids: list) -> dict:
    """A few real spellings per candidate, as the evidence the model reads.

    The canonical name alone is not enough to judge by, because the wrong merges
    are exactly the ones where it is misleading: an entity named "ยิ่งชีพ
    อัชฌานนท์" whose recorded spellings are "iLaw" and "ไอลอว์" is an
    organisation, and only the spellings say so.
    """
    from sqlalchemy import select

    from ..models import EventEntity

    if not entity_ids:
        return {}
    rows = (
        await session.execute(
            select(EventEntity.entity_id, EventEntity.surface_form)
            .where(EventEntity.entity_id.in_(entity_ids))
            .distinct()
        )
    ).all()
    forms: dict = {}
    for entity_id, surface in rows:
        forms.setdefault(entity_id, []).append(surface)
    return forms


#: Words that mark a model overriding evidence it has just acknowledged, or
#: declining to commit. Contrast words are deliberately absent: "ชื่อใหม่คือบุคคล
#: **แต่** ตัวเลือกคือองค์กร" is a confident refusal, not a hedge, and treating
#: "แต่" as doubt would flag the clearest answers in the set.
#:
#: Scanned over `reason` only, never `counter`. `counter` is the field where
#: doubt is *supposed* to live — the model is asked there for the strongest case
#: against — so hedge words in it mean the instruction was followed, not that the
#: answer is shaky. Measured both ways: including it caught nothing extra (both
#: errors in the hard set were carried by `reason` alone) while adding a class of
#: false alarm that exists by construction.
HEDGES = (
    "แม้", "อย่างไรก็ตาม", "น่าจะ", "อาจ", "ไม่แน่ใจ", "ควรระวัง", "คาดว่า",
    "เป็นไปได้ว่า", "ไม่ชัดเจน", "however", "possibly", "unclear",
)


def hedged(reason: str) -> bool:
    """Did the model qualify its own decision in prose?

    Takes the decision sentence alone. Passing the counter-argument in as well
    would ask "did it write down a doubt?", which it was instructed to do.
    """
    return any(marker in (reason or "") for marker in HEDGES)


@dataclass(frozen=True)
class LinkDecision:
    """One linking answer and the three things that say how much to trust it.

    Deliberately not one number. Self-reported confidence comes back 1.0 in 98%
    of answers on this corpus, so it is a restatement of the answer rather than a
    measurement of it. These three can each be wrong, but each is checkable:
    `agreed` is two answers compared, `counter` and `hedged` are prose the model
    wrote, and `confidence` is kept only because it costs nothing to carry.
    """

    match: int | None
    confidence: float
    reason: str
    counter: str = ""
    #: The same question asked with the two sides exchanged gave the same answer.
    agreed: bool = True
    hedged: bool = False
    failed: bool = False

    @property
    def is_certain(self) -> bool:
        return (
            not self.failed
            and self.agreed
            and not self.hedged
            and self.confidence >= REVIEW_THRESHOLD
        )


async def _ask(
    name: str,
    mentions: list[str],
    candidates: list[tuple[str, str, list[str]]],
    *,
    entity_type: str,
    context: str | None,
    client: OllamaClient,
    model: str | None,
    qualifiers: list[str] | None = None,
) -> dict | None:
    try:
        return await client.chat_json(
            entity_link_messages(
                name,
                mentions,
                candidates,
                entity_type=entity_type,
                context=context,
                qualifiers=qualifiers,
            ),
            purpose="entity_link",
            model=model,
        )
    except (OllamaError, ValueError) as exc:
        log.warning("entity link adjudication failed", extra={"error": str(exc)})
        return None


async def choose_existing(
    resolution: Resolution,
    candidates: list[tuple[str, str, list[str]]],
    *,
    context: str | None = None,
    client: OllamaClient | None = None,
    model: str | None = None,
) -> LinkDecision:
    """Which candidate this mention is, if any, and how much to trust the answer.

    Takes plain data rather than ORM rows so the decision can be tested without
    a database — this is the judgement the whole step exists to make, and it
    should not need Postgres running to check.

    Asked twice. The second time the two sides are exchanged: the candidate
    becomes the name in question and this mention becomes the only option.
    "Is the same thing as" is symmetric, so an answer that flips under the swap
    was never held firmly. Resampling would not show this — the client runs at
    temperature 0, so the identical prompt returns the identical answer — which
    is exactly why the perturbation has to change the question rather than repeat
    it.

    An index outside the list is refused rather than clamped: a model that names
    a candidate that was not offered has not read the list, and honouring it
    would merge into whatever happens to sit at that position.
    """
    if not candidates:
        return LinkDecision(None, 1.0, "ไม่มีตัวตนเดิมที่ใกล้เคียง")
    client = client or get_ollama()

    # The country in "กระทรวงการคลัง (สิงคโปร์)" is what makes it a different
    # ministry, and `classify` has already picked it out. Passed on rather than
    # discarded: the model had it inside a surface form and did not weigh it.
    qualifiers = sorted({q for raw in resolution.mentions for q in parse(raw).qualifiers})
    payload = await _ask(
        resolution.canonical,
        resolution.mentions,
        candidates,
        entity_type=resolution.entity_type,
        context=context,
        client=client,
        model=model,
        qualifiers=qualifiers,
    )
    if payload is None:
        return LinkDecision(None, 0.0, RISK_LINK_UNDECIDED, failed=True)

    raw = payload.get("match")
    match = raw if isinstance(raw, int) and 0 <= raw < len(candidates) else None
    counter = str(payload.get("counter") or "")
    reason = str(
        payload.get("reason")
        or ("ตรงกับตัวตนที่มีอยู่" if match is not None else "ไม่ตรงกับตัวตนใดที่มีอยู่")
    )

    # Swap against the candidate actually in play: the one just chosen, or the
    # strongest one if the answer was "none" — a wrong refusal is a duplicate,
    # and that is worth catching too.
    subject = candidates[match if match is not None else 0]
    mirror = await _ask(
        subject[0],
        subject[2] or [subject[0]],
        [(resolution.canonical, resolution.entity_type, resolution.mentions)],
        entity_type=subject[1],
        context=context,
        client=client,
        model=model,
    )
    if mirror is None:
        agreed = False
    else:
        mirror_says_same = mirror.get("match") == 0
        agreed = mirror_says_same == (match is not None)
        counter = " / ".join(x for x in (counter, str(mirror.get("counter") or "")) if x)

    return LinkDecision(
        match=match,
        confidence=_as_confidence(payload.get("confidence")),
        reason=reason,
        counter=counter,
        agreed=agreed,
        hedged=hedged(reason),
    )


def _as_confidence(value) -> float:
    """A missing or unparsable confidence means the model did not commit."""
    return float(value) if isinstance(value, (int, float)) else 0.5


def _why_unsure(decision: LinkDecision) -> str:
    """What the reviewer is being asked to check, and the case against.

    The model's own counter-argument goes in verbatim. It is the one piece of
    this that tells a reviewer *what to look at* rather than that something is
    off, and it was written before the decision, so it is not a rationalisation
    of one.
    """
    objections = []
    if not decision.agreed:
        objections.append("ถามกลับด้านแล้วตอบไม่ตรงกัน")
    if decision.hedged:
        objections.append("เหตุผลมีคำแบ่งรับแบ่งสู้")
    if decision.confidence < REVIEW_THRESHOLD:
        objections.append("โมเดลบอกเองว่าไม่มั่นใจ")
    why = " · ".join(objections) or "ไม่ระบุ"
    counter = decision.counter.strip()
    return f"{why} · ข้อค้าน: {counter[:200]}" if counter else why


async def persist(
    session,
    event_id,
    resolutions: list[Resolution],
    *,
    context: str | None = None,
    client: OllamaClient | None = None,
    model: str | None = None,
) -> dict[str, int]:
    """Write resolutions as entities and link them to the event.

    The cross-event merge happens here, and it is the one that matters: an
    article rarely spells a name two ways, but "the same person as last Tuesday"
    is a claim about the world. It is now made by the model with the article in
    hand, over candidates an index merely suggested.

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

        nearby = await _candidates(session, resolution.aliases)
        forms = await _surface_forms_of(session, [row.id for row in nearby])
        decision = await choose_existing(
            resolution,
            [(row.canonical_name, row.entity_type, forms.get(row.id, [])) for row in nearby],
            context=context,
            client=client,
            model=model,
        )
        entity = nearby[decision.match] if decision.match is not None else None
        if decision.failed:
            # The model could not be reached and candidates were on the table.
            # Refusing to merge makes a duplicate; merging on the index alone
            # makes false history. The repair pass can undo the first and nobody
            # can undo the second, so this is not a close call.
            link_risk = RISK_LINK_UNDECIDED
        elif entity is not None and (not decision.agreed or decision.hedged):
            # The model contradicted itself — the mirror question answered
            # differently, or the prose overrode evidence the same answer had
            # just acknowledged. Both were measured against 26 correct answers
            # without firing once, and together they caught both errors in the
            # hard set, so the link is refused rather than merely queued.
            # 26 is a small sample and this threshold is worth re-measuring as
            # the store grows; what makes it the safe side either way is that a
            # duplicate can be merged later and a merge cannot be undone.
            log.info(
                "refusing a link the model contradicted itself on",
                extra={"entity_name": entity.canonical_name, "against": decision.counter},
            )
            link_risk = f"{RISK_LINK_CONTRADICTED} ({_why_unsure(decision)})"
            entity = None
            decision = replace(decision, match=None)
        elif entity is not None and not decision.is_certain:
            # Only self-reported confidence is left, and it has never once been
            # seen to fire on a wrong answer. Too weak to refuse on, kept because
            # queueing costs nothing.
            link_risk = f"{RISK_UNSURE_LINK} ({_why_unsure(decision)})"
        else:
            link_risk = None
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
                risk=resolution.review_risk,
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
            log.info(
                "linked to an existing entity",
                extra={
                    "entity_name": entity.canonical_name,
                    "why": decision.reason,
                    "against": decision.counter,
                    "agreed": decision.agreed,
                },
            )

        entity.mention_count += len(resolution.mentions)
        entity.last_seen = datetime.now(UTC)
        risk = resolution.risk or link_risk
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
