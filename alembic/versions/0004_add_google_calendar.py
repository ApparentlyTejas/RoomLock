"""add Google Calendar sync columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09

Executes db/004_add_google_calendar.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "004_add_google_calendar.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS google_event_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS google_refresh_token")
