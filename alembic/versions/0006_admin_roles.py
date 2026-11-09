"""add is_admin to users

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09

Executes db/006_admin_roles.sql verbatim.
"""
from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[2] / "db"


def upgrade() -> None:
    op.execute((_SQL_DIR / "006_admin_roles.sql").read_text())


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_admin")
