"""allow NULL hashed_password for Google-only accounts

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09

Executes db/005_google_login.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "005_google_login.sql").read_text())


def downgrade() -> None:
    # Best-effort: fails if any Google-only account (NULL hashed_password)
    # exists, same as it would running this ALTER by hand.
    op.execute("ALTER TABLE users ALTER COLUMN hashed_password SET NOT NULL")
