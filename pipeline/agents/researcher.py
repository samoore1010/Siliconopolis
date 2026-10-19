"""Researcher: niches + research_notes + Etsy search (API only) -> product briefs (state=brief)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.db import session_scope
from pipeline.db.models import (
    BriefStatus,
    Product,
    ProductState,
    ProductType,
    ResearchNote,
    ResearchRequest,
    utcnow,
)
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.guardrails.caps import remaining_weekly_briefs

from .base import Agent

BRIEF_NICHE = "Owner brief"


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
        self.notes_text, self.etsy, self.blocklist = notes_text, etsy, blocklist
        self.etsy_search_ok = etsy_search_ok
        self.brief_block = ""
        self.added_ids: list[int] = []
        all_types = sorted({t for n in niches for t in n["product_types"]})

        # A queued run brief from the owner takes over this whole run (and only this run).
        brief = session.execute(select(ResearchRequest).where(ResearchRequest.status == BriefStatus.QUEUED)
                                .order_by(ResearchRequest.created_at.asc()).limit(1)).scalar_one_or_none()
        if brief is not None:
            self._run_brief(session, brief, all_types, remaining)
            session.add(ResearchNote(source="researcher", content=(
                f"Run {self.run_id}: owner brief #{brief.id} ({brief.focus[:120]}): created {self.items_processed} briefs.")))
            return

        per_niche = max(1, min(remaining, 20) // len(niches))
        for niche in niches:
            if self.should_stop():
                break
            if remaining <= 0:
                self.log.warning("weekly brief cap reached mid-run; stopping", cap=self.settings.weekly_brief_cap)
                break
            remaining -= self._research_niche(session, niche, min(per_niche, remaining))
            session.commit()  # keep each niche's ideas even if a later niche fails or you press Stop
            self.report_progress()

        session.add(ResearchNote(source="researcher", content=f"Run {self.run_id}{' (stopped early by the owner)' if self.stopped else ''}: created {self.items_processed} briefs across {len(niches)} niches."))

    def _run_brief(self, session: Session, brief: ResearchRequest, all_types: list[str], remaining: int) -> None:
        brief.status, brief.run_id, brief.started_at = BriefStatus.RUNNING, self.run_id, utcnow()
        types = [t for t in (brief.product_types or []) if t in all_types] or all_types
        n = max(1, min(int(brief.idea_count or 6), remaining, 20))
        parts = [p.strip() for p in re.split(r"[\n,;]+", brief.focus) if p.strip()]
        keywords = [re.sub(r"^(focus on|more|make|try|ideas for)\s+", "", p, flags=re.I)[:60] for p in parts][:3]
        self.brief_block = (f"OWNER RUN BRIEF (this whole run is dedicated to it; follow it over the niche list):\n"
                            f"{brief.focus}\n\n")
        focus = {"name": BRIEF_NICHE, "keywords": keywords or [brief.focus[:60]], "product_types": types,
                 "notes": "Ideas that carry out the owner's run brief. Each brief must include a short `niche` "
                          "name (e.g. 'halloween decor'); new niches are welcome."}
        try:
            self._research_niche(session, focus, n, brief_id=brief.id)
        except Exception as e:
            with session_scope() as s2:  # survives the rollback: the brief stays queued for the next run
                row = s2.get(ResearchRequest, brief.id)
                if row:
                    row.note = f"Run #{self.run_id} failed ({str(e)[:160]}); it will be retried on the next run."
            raise
        brief.status, brief.finished_at = BriefStatus.DONE, utcnow()
        brief.product_ids = [str(i) for i in self.added_ids]
        brief.note = ("" if len(self.added_ids) >= int(brief.idea_count or 0) else
                      f"Made {len(self.added_ids)} of {brief.idea_count} ideas (weekly idea limit or rejected ideas).")

    def _research_niche(self, session: Session, niche: dict, n: int, brief_id: int | None = None) -> int:
        """Research one niche (or the owner's run brief) and add up to `n` briefs. Returns briefs added."""
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
            f"{self.brief_block}{catalog_note}"
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
            "brief_id": brief_id,
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
            if niche_name == BRIEF_NICHE and str(brief.get("niche") or "").strip():
                niche_name = str(brief["niche"]).strip()[:120]
            product = Product(state=ProductState.BRIEF, niche=niche_name, product_type=ptype,
                              brief_json={**brief, "niche": niche_name, "run_id": self.run_id, "market": market_ref,
                                          **({"from_brief": brief_id} if brief_id else {})})
            product.add_cost(result.cost_usd / max(1, len(briefs)))
            session.add(product)
            session.flush()
            self.added_ids.append(product.id)
            self.items_processed += 1
            added += 1
        session.flush()
        self.log.info("briefs created", niche=niche.get("name"), count=added)
        return added
