"""Beat matching: serving a standing request, not reporting an anomaly."""

import uuid

import pytest

from horizon.batch.beats import MAX_MATCHES_PER_BEAT, BeatReport, is_candidate
from horizon.models import SIGNAL_TYPES, Beat


def _beat(categories):
    return Beat(id=uuid.uuid4(), name="ทดสอบ", description="อะไรก็ได้", categories=categories)


# ── what a beat is even asked about ─────────────────────────────────────────


def test_a_beat_with_no_categories_is_asked_about_everything():
    """Leaving categories blank means "I have not narrowed it", and it must not
    quietly come to mean "nothing" — that would make an empty-category beat
    silently dead, which is the failure this whole feature exists to remove."""
    assert is_candidate(_beat([]), ["บันเทิง/กีฬา"]) is True
    assert is_candidate(_beat(None), []) is True


def test_a_beat_with_categories_is_only_asked_about_those():
    assert is_candidate(_beat(["ต่างประเทศ"]), ["ต่างประเทศ", "ความมั่นคง"]) is True
    assert is_candidate(_beat(["ต่างประเทศ"]), ["บันเทิง/กีฬา"]) is False


def test_an_event_with_no_categories_reaches_only_unnarrowed_beats():
    """An event the extractor could not label must not be forced onto a beat
    that did narrow — but a beat that narrowed nothing still wants to see it."""
    assert is_candidate(_beat(["ต่างประเทศ"]), []) is False
    assert is_candidate(_beat([]), []) is True


# ── the shape of the answer ─────────────────────────────────────────────────


def test_beat_match_is_its_own_signal_type():
    """Not a flag on a detection. weak_signal and trend_breakout mean the engine
    noticed something; this means the newsroom asked for it, and a verdict of
    "not worth sending" means different things for the two."""
    assert "beat_match" in SIGNAL_TYPES


def test_a_report_says_what_it_did_with_nothing_to_do():
    report = BeatReport()

    assert report.as_log()["matched"] == 0
    assert report.as_log()["capped"] == []


def test_the_per_beat_cap_is_per_beat_not_per_run():
    """One loosely written beat must not be able to crowd a careful one out of
    the run — which a global cap would allow, silently and on whichever beat
    happened to be sorted first."""
    import inspect

    from horizon.batch import beats

    source = inspect.getsource(beats.run_beat_matching)

    assert "matched_here = 0" in source
    assert source.index("matched_here = 0") < source.index("for event_id")
    assert MAX_MATCHES_PER_BEAT > 0


@pytest.mark.parametrize("field", ["beat_id", "beat_name", "beat_reason"])
def test_the_payload_always_carries_the_beat_fields(field):
    """Present and null on a detection rather than absent, so the receiver never
    has to tell "not in this payload" from "not set"."""
    import inspect

    from horizon.reasoner import dispatch

    assert f'"{field}"' in inspect.getsource(dispatch.build_payload)


def test_the_embedding_shortlists_and_the_model_decides():
    """Asking the model about every (beat × event) pair was measured first: 4
    beats over one 12-hour window is ~1,300 questions on a 3-hour cycle. The
    vector search narrows the field; it must never be what accepts a match."""
    import inspect

    from horizon.batch import beats

    source = inspect.getsource(beats.run_beat_matching)

    # Shortlist comes from the vector store, and the decision from chat_json.
    assert "store.search(" in source
    assert source.index("store.search(") < source.index("chat_json")
    # No similarity threshold decides anything on its own — the model's answer
    # is the only thing that creates a match.
    assert 'answer.get("matches")' in source


def test_a_beat_that_cannot_be_shortlisted_does_not_cost_the_run():
    """Ollama or Qdrant being unavailable for one beat must leave the others
    working — the same rule the detectors already follow."""
    import inspect

    from horizon.batch import beats

    source = inspect.getsource(beats.run_beat_matching)
    shortlist_block = source[source.index("await store.search") : source.index("event_ids =")]

    assert "continue" in shortlist_block



def test_a_number_for_an_event_that_was_not_offered_is_dropped():
    """The model answers with positions in the list it was given. A number
    outside that list is ambiguous, and clamping it would file a real story
    under a reason written about a different one."""
    import inspect

    from horizon.batch import beats

    source = inspect.getsource(beats.run_beat_matching)

    assert "0 <= position < len(chunk)" in source
    assert source.index("0 <= position < len(chunk)") < source.index("BeatMatch(")


def test_a_match_nobody_heard_is_not_recorded_as_sent():
    """Redis pub/sub has no queue: publishing while the reasoner restarts
    succeeds, reaches nobody, and raises nothing. Observed live — the match row
    was written, so every later run skipped it as already handled and the story
    was gone. The row has to be rolled back."""
    import inspect

    from horizon.batch import beats

    source = inspect.getsource(beats.run_beat_matching)

    assert "if not heard:" in source
    assert "session.delete(stale)" in source
    # And the rollback has to happen before the counters say it was sent.
    assert source.index("session.delete(stale)") < source.index("report.matched += 1")


def test_publish_reports_how_many_heard_it_not_merely_that_it_tried():
    """A bool cannot tell "sent" from "shouted into an empty room", and callers
    write down "handled" on the strength of it."""
    import inspect

    from horizon.batch import signals

    assert "-> int" in inspect.getsource(signals.publish).splitlines()[0]
