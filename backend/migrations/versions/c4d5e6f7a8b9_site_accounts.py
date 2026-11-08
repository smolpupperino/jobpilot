"""site_accounts (job-site logins, encrypted passwords)

Revision ID: c4d5e6f7a8b9
Revises: b3c1d2e4f5a6
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c1d2e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site", sa.String(255), nullable=False, unique=True),
        sa.Column("login_email", sa.String(255), nullable=False),
        sa.Column("password_enc", sa.Text, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="creating"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("site_accounts")
