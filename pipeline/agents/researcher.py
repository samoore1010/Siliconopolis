"""Researcher: niches + research_notes + Etsy search (API only) -> product briefs (state=brief)."""

from __future__ import annotations

import json
from dataclasses import asdict

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.db.models import Product, ProductState, ProductType, ResearchNote
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.guardrails.caps import remaining_weekly_briefs

from .base import Agent


class ResearcherAgent(Agent):
    name = "researcher"
    prompt_file = "researcher"

    def load_niches(self) -> list[dict]:
        path = self.settings.resolve(self.settings.niches_file)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return list(data.get("niches", []))

    def execute(self, session: Session) -> None:
        remaining = remaining_weekly_briefs(session, self.settings.weekly_brief_cap)
        if remaining <= 0:
            raise CapReached("WEEKLY_BRIEF_CAP", self.settings.weekly_brief_cap, self.settings.weekly_brief_cap)

        niches = self.load_niches()
        if not niches:
            self.note_error("niches.yaml has no niches")
            return
        notes = session.execute(
            select(ResearchNote).order_by(ResearchNote.created_at.desc()).limit(5)
        ).scalars().all()
        notes_text = "\n\n".join(f"[{n.source} @ {n.created_at:%Y-%m-%d}] {n.content}" for n in notes) or "(none yet)"

        etsy = get_etsy_client()
        blocklist = get_blocklist()
        per_niche = max(1, min(remaining, 20) // len(niches))

        for niche in niches:
            if remaining <= 0:
                self.log.warning("weekly brief cap reached mid-run; stopping", cap=self.settings.weekly_brief_cap)
                break
            signals = []
            for kw in niche.get("keywords", [])[:3]:
                try:
                    for r in etsy.search_listings(kw, limit=10):
                        signals.append({"keywords": kw, **{k: v for k, v in asdict(r).items() if k in ("title", "price_usd", "num_favorers", "views", "tags")}})
                except Exception as e:  # noqa: BLE001
                    self.note_error(f"etsy search failed for {kw!r}: {e}")
            n = min(per_niche, remaining)
            user = (
                f"Niche config:\n{json.dumps({'niche': niche.get('name'), 'product_types': niche.get('product_types'), 'notes': niche.get('notes')}, indent=2)}\n\n"
                f"Recent analyst/research notes:\n{notes_text}\n\n"
                f"Demand signals from Etsy listing search (use only for keywords/price/popularity, never copy):\n"
                f"{json.dumps(signals[:30], indent=1)}\n\n"
                f"Produce exactly {n} original product briefs. Allowed product_type values: "
                f"{', '.join(niche.get('product_types') or list(ProductType.DIGITAL))}."
            )
            result = self.ask(session, task="research_briefs", model=self.settings.model_research, user=user)
            try:
                briefs = result.json().get("briefs", [])
            except Exception as e:  # noqa: BLE001
                self.note_error(f"researcher: bad JSON for niche {niche.get('name')}: {e}")
                continue
            allowed = set(niche.get("product_types") or ProductType.DIGITAL)
            for brief in briefs[:n]:
                ptype = brief.get("product_type", "")
                if ptype not in allowed or ptype not in ProductType.ALL:
                    self.log.warning("skipping brief with disallowed product_type", product_type=ptype)
                    continue
                hits = blocklist.check_many(json.dumps(brief))
                if hits:
                    self.log.warning("skipping brief: blocklist hit", hits=hits)
                    continue
                product = Product(state=ProductState.BRIEF, niche=str(niche.get("name")), product_type=ptype,
                                  brief_json={**brief, "niche": niche.get("name"), "run_id": self.run_id})
                product.add_cost(result.cost_usd / max(1, len(briefs)))
                session.add(product)
                self.items_processed += 1
                remaining -= 1
            session.flush()
            self.log.info("briefs created", niche=niche.get("name"), count=min(len(briefs), n))

        session.add(ResearchNote(source="researcher", content=f"Run {self.run_id}: created {self.items_processed} briefs across {len(niches)} niches."))
