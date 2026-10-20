"""Remove detection group counts from threshold rules.

Revision ID: 20261020_0028
Revises: 20261020_0027
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0028"
down_revision = "20261020_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_confirm_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_stuck_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_offline_points,
          DROP COLUMN IF EXISTS confirm_points,
          DROP COLUMN IF EXISTS stuck_points,
          DROP COLUMN IF EXISTS offline_points
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS confirm_points INTEGER NOT NULL DEFAULT 3,
          ADD COLUMN IF NOT EXISTS stuck_points INTEGER NOT NULL DEFAULT 100,
          ADD COLUMN IF NOT EXISTS offline_points INTEGER NOT NULL DEFAULT 5
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_confirm_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_stuck_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_offline_points,
          ADD CONSTRAINT ck_alert_threshold_confirm_points
            CHECK (confirm_points > 0),
          ADD CONSTRAINT ck_alert_threshold_stuck_points
            CHECK (stuck_points > 0),
          ADD CONSTRAINT ck_alert_threshold_offline_points
            CHECK (offline_points > 0)
        """
    )
