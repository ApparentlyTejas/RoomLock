"""add series_id to bookings for recurring series

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09

Executes db/008_recurring_bookings.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "008_recurring_bookings.sql").read_text())


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_bookings_series_id")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS series_id")
