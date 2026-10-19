"""owner_notes: directives, product comments and revision requests from the shop owner

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_owner_notes_kind", "owner_notes", ["kind"])
    op.create_index("ix_owner_notes_product_id", "owner_notes", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_owner_notes_product_id", table_name="owner_notes")
    op.drop_index("ix_owner_notes_kind", table_name="owner_notes")
    op.drop_table("owner_notes")
