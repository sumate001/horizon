"""The feed endpoints OSINT//DESK reads.

These need a database. Horizon's other tests are pure, so rather than add a
fixture nobody else wants, this module builds its own session and skips when
Postgres is not reachable — the same deal the DESK side makes.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from horizon.config import get_settings
from horizon.db import get_session
from horizon.models import Event
from horizon.services.api import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    """One transaction per test, rolled back — nothing reaches the real tables."""
    engine = create_async_engine(get_settings().postgres_url, poolclass=None)
    try:
        connection = await engine.connect()
    except Exception as exc:  # noqa: BLE001 — any connection failure means "no DB, skip"
        await engine.dispose()
        pytest.skip(f"no database: {exc}")

    transaction = await connection.begin()
    maker = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with maker() as db:
        yield db
    await transaction.rollback()
    await connection.close()
    await engine.dispose()


@pytest.fixture
async def client(session: AsyncSession):
    app.dependency_overrides[get_session] = lambda: session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def event(verdict: str | None, total: float | None = None) -> Event:
    return Event(
        id=uuid.uuid4(),
        summary=f"เหตุการณ์ทดสอบ {verdict}",
        actors=[],
        categories=[],
        triage_verdict=verdict,
        triage_total=total,
        created_at=datetime.now(UTC),
    )


async def test_counts_tally_each_verdict(session: AsyncSession, client: AsyncClient):
    before = (await client.get("/api/v1/events/counts")).json()
    session.add_all([event("PRIORITY", 8.0), event("PRIORITY", 7.9), event("PASS", 2.0)])
    await session.flush()

    counts = (await client.get("/api/v1/events/counts")).json()

    assert counts["PRIORITY"] - before.get("PRIORITY", 0) == 2
    assert counts["PASS"] - before.get("PASS", 0) == 1


async def test_all_counts_unscored_events_too(session: AsyncSession, client: AsyncClient):
    """The ALL tab lists every event, so its badge has to count every event.

    Summing the verdicts instead would label a tab "88" and then show 644 rows,
    which reads as a bug in the list rather than a gap in the scoring.
    """
    before = (await client.get("/api/v1/events/counts")).json()
    session.add_all([event("PRIORITY", 8.0), event(None), event(None)])
    await session.flush()

    counts = (await client.get("/api/v1/events/counts")).json()

    assert counts["ALL"] - before["ALL"] == 3
    assert counts["UNSCORED"] - before["UNSCORED"] == 2


async def test_the_list_shows_unscored_events_rather_than_hiding_them(
    session: AsyncSession, client: AsyncClient
):
    unscored = event(None)
    session.add(unscored)
    await session.flush()

    rows = (await client.get("/api/v1/events?limit=200")).json()

    listed = {row["id"] for row in rows}
    assert str(unscored.id) in listed


async def test_filtering_by_verdict_excludes_everything_else(
    session: AsyncSession, client: AsyncClient
):
    priority, passing = event("PRIORITY", 8.0), event("PASS", 2.0)
    session.add_all([priority, passing])
    await session.flush()

    rows = (await client.get("/api/v1/events?verdict=PRIORITY&limit=200")).json()

    listed = {row["id"] for row in rows}
    assert str(priority.id) in listed
    assert str(passing.id) not in listed


async def test_ordering_by_score_puts_the_top_story_first(
    session: AsyncSession, client: AsyncClient
):
    low, high = event("PASS", 1.0), event("PRIORITY", 9.99)
    session.add_all([low, high])
    await session.flush()

    rows = (await client.get("/api/v1/events?order=score&limit=200")).json()

    # Relative order only: the table already holds real events scoring 10.0,
    # so asserting on rows[0] would test the seed data, not the ordering.
    order = [row["id"] for row in rows]
    assert order.index(str(high.id)) < order.index(str(low.id))
