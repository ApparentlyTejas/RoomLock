"""add capacity and amenities to resources

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09

Executes db/007_resource_metadata.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "007_resource_metadata.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE resources DROP COLUMN IF EXISTS amenities")
    op.execute("ALTER TABLE resources DROP COLUMN IF EXISTS capacity")
