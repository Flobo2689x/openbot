"""An isolated git worktree per thread (runtime/worktrees.py).

Revision ID: 0017
Revises: 0016

This was 0016 until upstream's model catalog took that number. A database migrated with the old
numbering is stamped 0016 but has the worktree column and no model_catalog table, so both steps
only add what is missing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "model_catalog" not in inspector.get_table_names():
        op.create_table(
            "model_catalog",
            sa.Column("provider", sa.String(32), primary_key=True),
            sa.Column("models", sa.JSON(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "worktree" not in {c["name"] for c in inspector.get_columns("threads")}:
        op.add_column("threads", sa.Column("worktree", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("threads", "worktree")
