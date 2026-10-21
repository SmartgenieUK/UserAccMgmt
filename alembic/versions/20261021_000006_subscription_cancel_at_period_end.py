from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# BRIEF-029: a pending "cancel at end of billing period", so the account page can say when a plan ends.
revision = "20261021_000006"
down_revision = "20260914_000005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "subscriptions",
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade():
    op.drop_column("subscriptions", "cancel_at_period_end")
