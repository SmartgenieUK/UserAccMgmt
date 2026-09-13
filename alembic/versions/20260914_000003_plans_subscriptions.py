from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.models.plan import DEFAULT_PLANS

revision = "20260914_000003"
down_revision = "20260410_000002"
branch_labels = None
depends_on = None


def upgrade():
    plans = op.create_table(
        "plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("product", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("limits", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("product", "code", name="uq_plan_product_code"),
    )
    op.bulk_insert(plans, [{"id": uuid.uuid4(), **plan} for plan in DEFAULT_PLANS])

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product", sa.String(length=64), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.UniqueConstraint("org_id", "product", name="uq_subscription_org_product"),
    )
    op.create_index("ix_subscriptions_org_id", "subscriptions", ["org_id"])

    # Seat = a membership flagged per product.
    op.add_column(
        "memberships",
        sa.Column("products", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade():
    op.drop_column("memberships", "products")
    op.drop_index("ix_subscriptions_org_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
