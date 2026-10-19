"""fulfillment_orders: Etsy orders for in-house apparel and mugs, printed by the shop

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fulfillment_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("etsy_receipt_id", sa.String(64), nullable=False),
        sa.Column("etsy_transaction_id", sa.String(64), nullable=False, unique=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("item", sa.String(32), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("size", sa.String(32), nullable=True),
        sa.Column("color", sa.String(64), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("personalization_raw", sa.Text(), nullable=False, server_default=""),
        sa.Column("personalization", sa.JSON(), nullable=False),
        sa.Column("buyer_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("ship_to", sa.JSON(), nullable=False),
        sa.Column("print_file", sa.String(512), nullable=True),
        sa.Column("proof_image", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("carrier", sa.String(64), nullable=True),
        sa.Column("tracking_code", sa.String(128), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etsy_synced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("unit_cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("paid_to_shop", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_fulfillment_orders_receipt", "fulfillment_orders", ["etsy_receipt_id"])
    op.create_index("ix_fulfillment_orders_product_id", "fulfillment_orders", ["product_id"])
    op.create_index("ix_fulfillment_orders_status", "fulfillment_orders", ["status"])


def downgrade() -> None:
    for ix in ("ix_fulfillment_orders_status", "ix_fulfillment_orders_product_id", "ix_fulfillment_orders_receipt"):
        op.drop_index(ix, table_name="fulfillment_orders")
    op.drop_table("fulfillment_orders")
