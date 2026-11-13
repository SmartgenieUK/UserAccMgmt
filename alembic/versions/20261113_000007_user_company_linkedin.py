from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# CR-020 Lite sign-up: an optional company name and LinkedIn URL on the account (self-reported, never enriched).
revision = "20261113_000007"
down_revision = "20261021_000006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("company_name", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("linkedin_url", sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column("users", "linkedin_url")
    op.drop_column("users", "company_name")
