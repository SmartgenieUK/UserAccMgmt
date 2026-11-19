from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import UUID_TYPE, TZ_DATETIME

revision = "20261113_000009"
down_revision = "20261113_000008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tester_redemptions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("user_id", UUID_TYPE, nullable=False),
        sa.Column("org_id", UUID_TYPE, nullable=False),
        sa.Column("promotion_code_id", sa.String(64), nullable=False),
        sa.Column("tester_price_id", sa.String(64), nullable=False),
        sa.Column("stripe_customer_id", sa.String(64), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("started_at", TZ_DATETIME, nullable=False),
        sa.Column("ends_at", TZ_DATETIME, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_tester_redemption_user"),
        sa.UniqueConstraint("org_id", name="uq_tester_redemption_org"),
        sa.UniqueConstraint("stripe_subscription_id", name="uq_tester_redemption_subscription"),
    )


def downgrade():
    op.drop_table("tester_redemptions")
