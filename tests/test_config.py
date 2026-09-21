

# ── the source registry ─────────────────────────────────────────────────────


def test_every_seeded_source_url_is_unique():
    """seed() skips a source whose URL already exists, so a duplicate URL in the
    list means the second entry silently never gets created."""
    from horizon.seed import SOURCES

    urls = [url for _, url, _, _ in SOURCES]

    assert len(urls) == len(set(urls))


def test_seeded_credibility_weights_are_in_range():
    """ck_sources_credibility_range rejects anything outside 0..1, and seed runs
    after every migration — a bad weight would fail the deploy, not the tests."""
    from horizon.seed import SOURCES

    for name, _, _, credibility in SOURCES:
        assert 0.0 <= credibility <= 1.0, name


def test_the_registry_carries_both_thai_and_international_sources():
    """International feeds go through the same pipeline: the extraction prompt
    expects mixed Thai/English input and answers in Thai either way. If these
    ever get split onto a separate path, this is the assumption to revisit."""
    from horizon.seed import SOURCES

    names = {name for name, _, _, _ in SOURCES}

    assert {"ไทยรัฐ", "ข่าวสด"} <= names
    assert {"BBC World", "Nikkei Asia", "Al Jazeera"} <= names


def test_seeded_source_types_are_accepted_by_the_check_constraint():
    from horizon.models import SOURCE_TYPES
    from horizon.seed import SOURCES

    for name, _, source_type, _ in SOURCES:
        assert source_type in SOURCE_TYPES, name


def test_the_llm_timeout_survives_a_shared_gpu():
    """60s was calibrated against a model resident on the GPU. Measured on a box
    where a 31b model held 19.2GB of VRAM, gemma4:12b ran 72% on CPU and a
    trivial prompt took 94 seconds — every call timed out twice, and extraction
    failed on 39–72% of articles for over a week."""
    from horizon.config import Settings

    assert Settings().llm_timeout >= 120.0
