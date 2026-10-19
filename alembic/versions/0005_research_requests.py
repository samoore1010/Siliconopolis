"""research_requests: one-off run briefs from the owner (replace standing directives)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("focus", sa.Text(), nullable=False),
        sa.Column("product_types", sa.JSON(), nullable=False),
        sa.Column("idea_count", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("product_ids", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_research_requests_status", "research_requests", ["status"])
    op.create_index("ix_research_requests_run_id", "research_requests", ["run_id"])
    # Standing "directives" become one-off briefs for the next run (and stop applying forever).
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, text FROM owner_notes WHERE kind = 'directive' AND active = :t"),
                        {"t": True}).fetchall()
    now = datetime.now(timezone.utc)
    table = sa.table("research_requests", sa.column("created_at", sa.DateTime(timezone=True)),
                     sa.column("updated_at", sa.DateTime(timezone=True)), sa.column("focus", sa.Text()),
                     sa.column("product_types", sa.JSON()), sa.column("idea_count", sa.Integer()),
                     sa.column("status", sa.String()), sa.column("product_ids", sa.JSON()),
                     sa.column("note", sa.Text()))
    if rows:
        op.bulk_insert(table, [{"created_at": now, "updated_at": now, "focus": r.text, "product_types": [],
                                "idea_count": 6, "status": "queued", "product_ids": [],
                                "note": "Converted from a standing direction."} for r in rows])
        conn.execute(sa.text("UPDATE owner_notes SET active = :f WHERE kind = 'directive'"), {"f": False})


def downgrade() -> None:
    op.drop_index("ix_research_requests_run_id", table_name="research_requests")
    op.drop_index("ix_research_requests_status", table_name="research_requests")
    op.drop_table("research_requests")
