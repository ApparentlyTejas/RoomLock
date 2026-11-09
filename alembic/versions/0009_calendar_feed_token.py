"""add calendar_feed_token to users

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09

Executes db/009_calendar_feed_token.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "009_calendar_feed_token.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS calendar_feed_token")
