"""Researcher: niches + research_notes + Etsy search (API only) -> product briefs (state=brief)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.db.models import (
    OwnerNote,
    OwnerNoteKind,
    Product,
    ProductState,
    ProductType,
    ResearchNote,
    utcnow,
)
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.guardrails.caps import remaining_weekly_briefs

from .base import Agent

DIRECTIVE_NICHE = "Owner direction"
DIRECTIVE_SHARE = 0.5  # share of this run's ideas spent directly on the owner's directives


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
            select(ResearchNote).where(ResearchNote.source.in_(["analyst", "researcher"]))
            .order_by(ResearchNote.created_at.desc()).limit(5)
        ).scalars().all()
        notes_text = "\n\n".join(f"[{n.source} @ {n.created_at:%Y-%m-%d}] {n.content}" for n in notes) or "(none yet)"

        etsy = get_etsy_client()
        blocklist = get_blocklist()
        etsy_search_ok = True  # flips off after the first failure (e.g. API key still pending approval)
        # Without a real image provider, wall art would be placeholder images; hold it back on Railway.
        skip_types = set()
        if self.settings.image_provider == "mock" and self.settings.app_env != "local":
            skip_types.add(ProductType.WALL_ART)
        niches = [
            {**n, "product_types": [t for t in (n.get("product_types") or ProductType.DIGITAL) if t not in skip_types]}
            for n in niches
        ]
        held = [n.get("name") for n in niches if not n["product_types"]]
        if held:
            self.log.warning("skipping niches until a real image provider is configured", niches=held)
        niches = [n for n in niches if n["product_types"]]
        if not niches:
            self.log.warning("no niches eligible this run")
            return
        directives = session.execute(select(OwnerNote).where(OwnerNote.kind == OwnerNoteKind.DIRECTIVE,
                                                             OwnerNote.active.is_(True))
                                     .order_by(OwnerNote.id.asc())).scalars().all()
        directives_text = "\n".join(f"- {d.text}" for d in directives)
        self.directives_block = (f"OWNER DIRECTIVES (the shop owner's current instructions; they come first):\n"
                                 f"{directives_text}\n\n") if directives else ""
        self.notes_text, self.etsy, self.blocklist = notes_text, etsy, blocklist
        self.etsy_search_ok = etsy_search_ok
        budget = min(remaining, 20)
        all_types = sorted({t for n in niches for t in n["product_types"]})
        if directives:
            share = max(1, round(budget * DIRECTIVE_SHARE)) if len(niches) else budget
            keywords = [re.sub(r"^(focus on|more|make|try)\s+", "", d.text.strip(), flags=re.I)[:60] for d in directives][:3]
            focus = {"name": DIRECTIVE_NICHE, "keywords": keywords, "product_types": all_types,
                     "notes": "Ideas that carry out the owner's directives. Each brief must include a short "
                              "`niche` name (e.g. 'halloween decor'); new niches are welcome."}
            remaining -= self._research_niche(session, focus, min(share, remaining))
            for d in directives:
                d.used_at = utcnow()
            budget = max(0, budget - share)
        per_niche = max(1, budget // len(niches)) if budget else 0
        for niche in niches:
            if remaining <= 0 or per_niche <= 0:
                if remaining <= 0:
                    self.log.warning("weekly brief cap reached mid-run; stopping", cap=self.settings.weekly_brief_cap)
                break
            remaining -= self._research_niche(session, niche, min(per_niche, remaining))

        session.add(ResearchNote(source="researcher", content=f"Run {self.run_id}: created {self.items_processed} briefs across {len(niches)} niches."))

    def _research_niche(self, session: Session, niche: dict, n: int) -> int:
        """Research one niche (or the owner's directives) and add up to `n` briefs. Returns briefs added."""
        added = 0
        signals = []
        for kw in niche.get("keywords", [])[:3]:
            if not self.etsy_search_ok:
                break
            try:
                for r in self.etsy.search_listings(kw, limit=10):
                    signals.append({"keywords": kw, **{k: v for k, v in asdict(r).items() if k in ("title", "price_usd", "num_favorers", "views", "tags")}})
            except Exception as e:  # noqa: BLE001
                self.etsy_search_ok = False
                self.log.warning("etsy search unavailable; researching from niche keywords only this run",
                                 keyword=kw, error=str(e)[:200])
        catalog_note = ""
        physical = [t for t in niche.get("product_types") or [] if t in ProductType.PHYSICAL]
        if physical:
            from pipeline.catalog import get_catalog

            items = [i.summary() for t in physical for i in get_catalog().for_type(t)]
            catalog_note = (f"In-house products our print shop can make (each physical brief must name one `item`):\n"
                            f"{json.dumps(items, indent=1)}\n\n")
        user = (
            f"{self.directives_block}{catalog_note}"
            f"Niche config:\n{json.dumps({'niche': niche.get('name'), 'product_types': niche.get('product_types'), 'notes': niche.get('notes')}, indent=2)}\n\n"
            f"Recent analyst/research notes:\n{self.notes_text}\n\n"
            f"Demand signals from Etsy listing search (use only for keywords/price/popularity, never copy):\n"
            f"{json.dumps(signals[:30], indent=1) if signals else '(Etsy search unavailable this run; rely on the niche keywords: ' + ', '.join(niche.get('keywords', [])) + ')'}\n\n"
            f"Produce exactly {n} original product briefs. Allowed product_type values: "
            f"{', '.join(niche.get('product_types') or list(ProductType.DIGITAL))}."
        )
        result = self.ask(session, task="research_briefs", model=self.settings.model_research, user=user,
                          max_tokens=16000)
        try:
            reply = result.json()
            briefs = reply.get("briefs", [])
        except Exception as e:  # noqa: BLE001
            self.note_error(f"researcher: bad JSON for niche {niche.get('name')}: {e}")
            return 0
        analysis = reply.get("market_analysis") or {}
        market_note = ResearchNote(source="market", content=json.dumps({
            "niche": niche.get("name"), "run_id": self.run_id, "analysis": analysis,
            "etsy_data": bool(signals), "signals_count": len(signals),
            "signals_sample": sorted(signals, key=lambda x: -(x.get("num_favorers") or 0))[:8],
            "keywords": niche.get("keywords", []), "product_types": niche.get("product_types"),
            "directives": bool(self.directives_block),
        }))
        session.add(market_note)
        session.flush()
        market_ref = {"note_id": market_note.id, "summary": analysis.get("summary"),
                      "price_range": analysis.get("price_range"), "data_basis": analysis.get("data_basis"),
                      "etsy_data": bool(signals)}
        allowed = set(niche.get("product_types") or ProductType.DIGITAL)
        for brief in briefs[:n]:
            if not isinstance(brief, dict):
                continue
            ptype = brief.get("product_type", "")
            if ptype not in allowed or ptype not in ProductType.ALL:
                self.log.warning("skipping brief with disallowed product_type", product_type=ptype)
                continue
            hits = self.blocklist.check_many(json.dumps(brief))
            if hits:
                self.log.warning("skipping brief: blocklist hit", hits=hits)
                continue
            niche_name = str(niche.get("name"))
            if niche_name == DIRECTIVE_NICHE and str(brief.get("niche") or "").strip():
                niche_name = str(brief["niche"]).strip()[:120]
            product = Product(state=ProductState.BRIEF, niche=niche_name, product_type=ptype,
                              brief_json={**brief, "niche": niche_name, "run_id": self.run_id, "market": market_ref,
                                          **({"from_directive": True} if niche.get("name") == DIRECTIVE_NICHE else {})})
            product.add_cost(result.cost_usd / max(1, len(briefs)))
            session.add(product)
            self.items_processed += 1
            added += 1
        session.flush()
        self.log.info("briefs created", niche=niche.get("name"), count=added)
        return added
