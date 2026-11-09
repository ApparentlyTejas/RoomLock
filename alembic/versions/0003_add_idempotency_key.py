"""add idempotency_key to bookings

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09

See docs/adr/0002-advisory-lock-for-idempotency-key-duplicates.md.
Executes db/003_add_idempotency_key.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "003_add_idempotency_key.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_user_idempotency_key_unique")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS idempotency_key")
