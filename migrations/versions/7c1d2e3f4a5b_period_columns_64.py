"""period columns 16 -> 64 chars (line markets: "regular_time|total=2.5")

Revision ID: 7c1d2e3f4a5b
Revises: 53bf3e932cf4
Create Date: 2026-11-19 17:20:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c1d2e3f4a5b"
down_revision: Union[str, Sequence[str], None] = "53bf3e932cf4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("arbitrage_opportunities", "matched_events"):
        with op.batch_alter_table(table) as b:
            b.alter_column("period", existing_type=sa.String(16), type_=sa.String(64), existing_nullable=False)


def downgrade() -> None:
    for table in ("arbitrage_opportunities", "matched_events"):
        with op.batch_alter_table(table) as b:
            b.alter_column("period", existing_type=sa.String(64), type_=sa.String(16), existing_nullable=False)
