from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# CR-020 / BRIEF-183: every existing account must complete Welcome, including paid accounts.
revision = "20261113_000008"
down_revision = "20261113_000007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("first_name", sa.String(length=80), nullable=True))
    op.add_column("users", sa.Column("last_name", sa.String(length=80), nullable=True))
    op.add_column("users", sa.Column("role", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("country", sa.String(length=2), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("github_url", sa.String(length=255), nullable=True))
    # The server default backfills every legacy row with false and applies to new inserts too.
    op.add_column("users", sa.Column("profile_completed", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("users", "profile_completed")
    op.drop_column("users", "github_url")
    op.drop_column("users", "phone")
    op.drop_column("users", "country")
    op.drop_column("users", "role")
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
