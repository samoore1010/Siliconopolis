"""SQLAlchemy models: products (queue), research_notes, runs, oauth_tokens."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict] = {dict[str, Any]: JSON, list[str]: JSON}


class ProductState:
    BRIEF = "brief"
    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    DRAFTED = "drafted"
    PUBLISHED = "published"  # set by Analyst when it observes the owner published it on Etsy
    RETIRED = "retired"
    ALL = (BRIEF, CREATED, APPROVED, REJECTED, DRAFTED, PUBLISHED, RETIRED)


class ProductType:
    WALL_ART = "wall_art"
    PLANNER = "planner"
    TRACKER = "tracker"
    SPREADSHEET = "spreadsheet"
    POD_POSTER = "pod_poster"
    POD_STICKER = "pod_sticker"
    POD_MUG = "pod_mug"
    POD_SHIRT = "pod_shirt"
    APPAREL = "apparel"  # in-house: shirts, sweatshirts, hoodies (catalog.yaml)
    MUG = "mug"          # in-house: printed mugs (catalog.yaml)
    DIGITAL = (WALL_ART, PLANNER, TRACKER, SPREADSHEET)
    PHYSICAL = (APPAREL, MUG)
    ALL = DIGITAL + PHYSICAL + (POD_POSTER, POD_STICKER, POD_MUG, POD_SHIRT)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=ProductState.BRIEF, index=True)
    niche: Mapped[str] = mapped_column(String(128), nullable=False)
    product_type: Mapped[str] = mapped_column(String(32), nullable=False)

    brief_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    file_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    image_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    qa_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    listing_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    etsy_listing_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    printify_product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # perceptual hash of the primary image, for near-duplicate detection
    phash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # row-level claim lock (compare-and-swap), see pipeline/db/locking.py
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_products_state_locked", "state", "locked_by"),)

    def add_cost(self, usd: float) -> None:
        self.cost_usd = float(self.cost_usd or 0.0) + float(usd)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product id={self.id} state={self.state} type={self.product_type} niche={self.niche!r}>"


class ResearchNote(Base):
    __tablename__ = "research_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # analyst | researcher
    content: Mapped[str] = mapped_column(Text, nullable=False)


class RunStatus:
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CAP_REACHED = "cap_reached"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RunStatus.RUNNING)
    items_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # token / image usage so cost can be re-derived if pricing changes
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class OAuthToken(Base):
    """Persisted OAuth tokens. Etsy rotates the refresh token on every refresh, so env vars only seed this."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)  # "etsy"
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Event(Base):
    """Append-only activity log used to animate the village UI (run start/end, transitions, caps, errors)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    agent: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="transition")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class OwnerNoteKind:
    DIRECTIVE = "directive"  # shop-wide direction for the Researcher, e.g. "focus on Halloween decor"
    COMMENT = "comment"      # a note on one product
    REVISION = "revision"    # a note on one product that sends it back to the Creator
    ALL = (DIRECTIVE, COMMENT, REVISION)


class OwnerNote(Base):
    """Input from the shop owner: shop-wide directives and per-product comments / revision requests."""

    __tablename__ = "owner_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # revision lifecycle: open -> addressed (Creator rebuilt it); directives: last time the Researcher used it
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrderStatus:
    NEW = "new"                    # just seen on Etsy
    NEEDS_REVIEW = "needs_review"  # personalisation unclear/inappropriate, or the print file failed: owner decides
    READY = "ready"                # print file ready; waiting for the shop
    PRINTING = "printing"
    SHIPPED = "shipped"            # shop entered tracking; sent to Etsy when etsy_synced is set
    CANCELLED = "cancelled"
    OPEN = (NEW, NEEDS_REVIEW, READY, PRINTING)
    ALL = (NEW, NEEDS_REVIEW, READY, PRINTING, SHIPPED, CANCELLED)


class FulfillmentOrder(Base):
    """One Etsy order line for an in-house product, from purchase to tracking number."""

    __tablename__ = "fulfillment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    etsy_receipt_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    etsy_transaction_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    item: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size: Mapped[str | None] = mapped_column(String(32), nullable=True)
    color: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    personalization_raw: Mapped[str] = mapped_column(Text, nullable=False, default="")
    personalization: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    buyer_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ship_to: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    print_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    proof_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=OrderStatus.NEW, index=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tracking_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etsy_synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unit_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # owed to the shop per item
    paid_to_shop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    @property
    def owed(self) -> float:
        return round(float(self.unit_cost or 0) * int(self.quantity or 1), 2)


class BriefStatus:
    QUEUED = "queued"        # waiting for the next research run
    RUNNING = "running"      # a research run is using it right now
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ResearchRequest(Base):
    """A run brief from the owner: steers exactly ONE research run, then is archived with its results."""

    __tablename__ = "research_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    focus: Mapped[str] = mapped_column(Text, nullable=False)
    product_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)  # [] = any we make
    idea_count: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=BriefStatus.QUEUED, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    product_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
