"""initial schema (users, resources, bookings)

Revision ID: 0001
Revises:
Create Date: 2026-11-09

Executes db/001_schema.sql verbatim rather than redefining the DDL here, so
that file stays the single source of truth for what each migration does
(it's also what the ADRs and CONTRIBUTING.md reference by name).
"""
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "001_schema.sql").read_text())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bookings")
    op.execute("DROP TABLE IF EXISTS resources")
    op.execute("DROP TABLE IF EXISTS users")
