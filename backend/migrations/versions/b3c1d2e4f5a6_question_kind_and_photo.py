"""owner_questions: kind (fact/approval/takeover) and photo_path

Revision ID: b3c1d2e4f5a6
Revises: 70fd0c8a1f44
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c1d2e4f5a6"
down_revision = "70fd0c8a1f44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("owner_questions") as b:
        b.add_column(sa.Column("kind", sa.String(16), nullable=False, server_default="fact"))
        b.add_column(sa.Column("photo_path", sa.String(300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("owner_questions") as b:
        b.drop_column("photo_path")
        b.drop_column("kind")
