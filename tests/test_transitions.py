from datetime import UTC

from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import Product, ProductState


def test_claim_is_exclusive(session, brief_factory):
    for _ in range(3):
        brief_factory(session)
    session.commit()
    first = claim_products(session, state=ProductState.BRIEF, owner="run-a", limit=2)
    second = claim_products(session, state=ProductState.BRIEF, owner="run-b", limit=5)
    assert len(first) == 2 and len(second) == 1
    assert {p.id for p in first}.isdisjoint({p.id for p in second})
    assert claim_products(session, state=ProductState.BRIEF, owner="run-c", limit=5) == []


def test_stale_lock_is_reclaimable(session, brief_factory):
    from datetime import datetime, timedelta

    p = brief_factory(session)
    p.locked_by, p.locked_at = "dead-run", datetime.now(UTC) - timedelta(hours=5)
    session.commit()
    assert claim_products(session, state=ProductState.BRIEF, owner="x", limit=1, lock_timeout_minutes=120)
    assert claim_products(session, state=ProductState.BRIEF, owner="y", limit=1, lock_timeout_minutes=120) == []


def test_transition_releases_lock(session, brief_factory):
    p = brief_factory(session)
    session.commit()
    [claimed] = claim_products(session, state=ProductState.BRIEF, owner="r", limit=1)
    transition(session, claimed, ProductState.CREATED)
    session.commit()
    fresh = session.get(Product, p.id)
    assert fresh.state == ProductState.CREATED and fresh.locked_by is None
    release(session, fresh)


def test_creator_rejects_pod_types_in_v1(session, brief_factory):
    from pipeline.agents.creator import CreatorAgent

    brief_factory(session, product_type="pod_poster")
    session.commit()
    outcome = CreatorAgent().run()
    assert outcome.items_processed == 0 and outcome.errors
    with __import__("pipeline.db", fromlist=["session_scope"]).session_scope() as s:
        p = s.query(Product).one()
        assert p.state == ProductState.BRIEF and p.locked_by is None  # released for retry
