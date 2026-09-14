from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260914_000005"
down_revision = "20260914_000004"
branch_labels = None
depends_on = None


def upgrade():
    # Add stripe_customer_id to organizations
    op.add_column("organizations", sa.Column("stripe_customer_id", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_organizations_stripe_customer_id"), "organizations", ["stripe_customer_id"], unique=True)

    # Create stripe_events table for idempotency
    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade():
    op.drop_table("stripe_events")
    op.drop_index(op.f("ix_organizations_stripe_customer_id"), table_name="organizations")
    op.drop_column("organizations", "stripe_customer_id")
