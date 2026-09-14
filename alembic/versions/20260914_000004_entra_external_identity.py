from __future__ import annotations

from alembic import op

# W3: register the Entra External ID provider and make one external subject link to at most one user.
revision = "20260914_000004"
down_revision = "20260914_000003"
branch_labels = None
depends_on = None


def upgrade():
    # Add the new provider to the native PG enum (no-op if a rerun); harmless on backends without the type.
    op.execute("ALTER TYPE external_provider ADD VALUE IF NOT EXISTS 'entra_external'")
    op.create_unique_constraint(
        "uq_external_provider_sub", "external_identities", ["provider", "provider_user_id"]
    )


def downgrade():
    op.drop_constraint("uq_external_provider_sub", "external_identities", type_="unique")
    # PostgreSQL cannot drop a single enum value; leaving 'entra_external' in place is harmless.
