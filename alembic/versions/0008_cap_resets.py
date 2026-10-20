"""cap_resets: the owner's manual resets of the daily spend / weekly ideas limits

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cap_resets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cap", sa.String(16), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_cap_resets_cap", "cap_resets", ["cap"])


def downgrade() -> None:
    op.drop_index("ix_cap_resets_cap", table_name="cap_resets")
    op.drop_table("cap_resets")
