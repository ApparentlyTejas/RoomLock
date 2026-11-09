"""add series_idempotency_key to bookings

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09

Executes db/010_recurring_series_idempotency_key.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "010_recurring_series_idempotency_key.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_user_series_idempotency_key_unique")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS series_idempotency_key")
