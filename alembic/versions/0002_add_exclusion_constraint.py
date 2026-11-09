"""add GiST exclusion constraint to prevent overlapping bookings

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09

See docs/adr/0001-postgres-exclusion-constraint-for-booking-overlaps.md.
Executes db/002_add_exclusion_constraint.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "002_add_exclusion_constraint.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_no_overlap")
