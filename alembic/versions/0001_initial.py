"""initial schema: products, research_notes, runs, oauth_tokens

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("niche", sa.String(128), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("brief_json", sa.JSON(), nullable=False),
        sa.Column("file_paths", sa.JSON(), nullable=False),
        sa.Column("image_paths", sa.JSON(), nullable=False),
        sa.Column("qa_result_json", sa.JSON(), nullable=True),
        sa.Column("listing_json", sa.JSON(), nullable=True),
        sa.Column("etsy_listing_id", sa.String(64), nullable=True),
        sa.Column("printify_product_id", sa.String(64), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("phash", sa.String(64), nullable=True),
        sa.Column("locked_by", sa.String(64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_products_state", "products", ["state"])
    op.create_index("ix_products_etsy_listing_id", "products", ["etsy_listing_id"])
    op.create_index("ix_products_phash", "products", ["phash"])
    op.create_index("ix_products_state_locked", "products", ["state", "locked_by"])

    op.create_table(
        "research_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("items_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("usage_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_runs_agent", "runs", ["agent"])

    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(32), nullable=False, unique=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oauth_tokens")
    op.drop_index("ix_runs_agent", table_name="runs")
    op.drop_table("runs")
    op.drop_table("research_notes")
    for ix in ("ix_products_state_locked", "ix_products_phash", "ix_products_etsy_listing_id", "ix_products_state"):
        op.drop_index(ix, table_name="products")
    op.drop_table("products")
