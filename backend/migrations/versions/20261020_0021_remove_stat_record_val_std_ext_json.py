"""Remove val_std and ext_json from t_stat_record.

Revision ID: 20261020_0021
Revises: 20261020_0020
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0021"
down_revision = "20261020_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP COLUMN IF EXISTS val_std,
          DROP COLUMN IF EXISTS ext_json
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD COLUMN IF NOT EXISTS val_std NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS ext_json JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
