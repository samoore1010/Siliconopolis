"""job_requests: "Run now" requests picked up by the in-app worker

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="owner"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_job_requests_agent", "job_requests", ["agent"])
    op.create_index("ix_job_requests_status", "job_requests", ["status"])


def downgrade() -> None:
    op.drop_index("ix_job_requests_status", table_name="job_requests")
    op.drop_index("ix_job_requests_agent", table_name="job_requests")
    op.drop_table("job_requests")
