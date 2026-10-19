"""Analyst: Etsy receipts + listing stats + our DB -> research_notes + weekly summary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.db.models import Product, ProductState, ResearchNote, Run

from .base import Agent

DEAD_AFTER_DAYS = 30


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AnalystAgent(Agent):
    name = "analyst"
    prompt_file = "analyst"

    def execute(self, session: Session) -> None:
        etsy = get_etsy_client()
        drafted = session.execute(
            select(Product).where(Product.state.in_([ProductState.DRAFTED, ProductState.PUBLISHED]),
                                  Product.etsy_listing_id.is_not(None))
        ).scalars().all()
        listing_ids = [p.etsy_listing_id for p in drafted if p.etsy_listing_id]

        stats, receipts = [], []
        if not etsy.is_connected():
            self.log.warning("Etsy not connected yet; skipping listing stats and receipts this run")
        else:
            try:
                stats = etsy.get_listing_stats(listing_ids[:100]) if listing_ids else []
                since = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
                receipts = etsy.get_shop_receipts(min_created=since)
            except Exception as e:  # noqa: BLE001
                self.note_error(f"analyst: etsy read failed: {e}")

        # Detect listings the owner published (human step) and mirror state
        by_listing = {p.etsy_listing_id: p for p in drafted}
        for st in stats:
            p = by_listing.get(st.listing_id)
            if p and st.state == "active" and p.state == ProductState.DRAFTED:
                p.state = ProductState.PUBLISHED  # observed on Etsy, set by the human
                self.log.info("observed owner published listing", product_id=p.id, listing_id=st.listing_id)

        sales_by_listing: dict[str, int] = {}
        for r in receipts:
            for lid in r.listing_ids:
                sales_by_listing[lid] = sales_by_listing.get(lid, 0) + 1

        counts = dict(session.execute(select(Product.state, func.count(Product.id)).group_by(Product.state)).all())
        week_ago = datetime.now(UTC) - timedelta(days=7)
        spend = float(session.execute(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.started_at >= week_ago)).scalar_one())
        cutoff = datetime.now(UTC) - timedelta(days=DEAD_AFTER_DAYS)
        stat_map = {s.listing_id: s for s in stats}
        catalog = []
        for p in drafted:
            s = stat_map.get(p.etsy_listing_id)
            catalog.append({"product_id": p.id, "niche": p.niche, "type": p.product_type, "state": p.state,
                            "price": (p.listing_json or {}).get("price"), "views": s.views if s else None,
                            "favorites": s.num_favorers if s else None, "sales": sales_by_listing.get(p.etsy_listing_id, 0),
                            "age_days": (datetime.now(UTC) - _aware(p.updated_at)).days,
                            "stale": _aware(p.updated_at) < cutoff and not (s and (s.views or s.num_favorers))})
        rejections = session.execute(
            select(Product.niche, Product.product_type, Product.qa_result_json).where(Product.state == ProductState.REJECTED).limit(20)
        ).all()
        user = (f"Catalog counts by state: {json.dumps(counts)}\nSpend last 7 days (USD): {spend:.2f}\n\n"
                f"Drafted/published listings with stats:\n{json.dumps(catalog, indent=1, default=str)}\n\n"
                f"Receipts last 30 days: {len(receipts)}\n\n"
                f"Recent QA rejections (niche, type, reasons):\n{json.dumps([[n, t, (q or {}).get('reasons')] for n, t, q in rejections], indent=1)}")
        result = self.ask(session, task="analyst_summary", model=self.settings.model_research, user=user)
        analysis = result.json()
        note = (f"WEEKLY SUMMARY (run {self.run_id})\n{analysis.get('summary', '')}\n\n"
                f"Guidance for Researcher:\n{analysis.get('guidance_for_researcher', '')}\n\n"
                f"Top performers: {json.dumps(analysis.get('top_performers', []))}\n"
                f"Dead listings flagged: {json.dumps(analysis.get('dead_listings', []))}\n"
                f"Stale (>{DEAD_AFTER_DAYS}d, no engagement): {[c['product_id'] for c in catalog if c['stale']]}")
        session.add(ResearchNote(source="analyst", content=note))
        self.items_processed = len(drafted)
        self.log.info("analyst summary written", drafted=len(drafted), receipts=len(receipts), spend_7d=round(spend, 2))
