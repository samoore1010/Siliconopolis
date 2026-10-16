"""events table for the village UI

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="transition"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_events_ts", "events", ["ts"])
    op.create_index("ix_events_agent", "events", ["agent"])
    op.create_index("ix_events_product_id", "events", ["product_id"])


def downgrade() -> None:
    for ix in ("ix_events_product_id", "ix_events_agent", "ix_events_ts"):
        op.drop_index(ix, table_name="events")
    op.drop_table("events")
