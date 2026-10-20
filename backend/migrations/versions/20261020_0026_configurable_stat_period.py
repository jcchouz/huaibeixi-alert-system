"""Allow configurable minute-based periods in t_stat_record.

Revision ID: 20261020_0026
Revises: 20261020_0025
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0026"
down_revision = "20261020_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP CONSTRAINT IF EXISTS ck_stat_record_period_type
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD CONSTRAINT ck_stat_record_period_type
          CHECK (period_type ~ '^[0-9]+m$')
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.period_type IS
          '统计周期，格式为分钟数加m，例如10m；由DETECTION_WINDOW_MINUTES配置'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP CONSTRAINT IF EXISTS ck_stat_record_period_type
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD CONSTRAINT ck_stat_record_period_type
          CHECK (period_type = '1m')
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.period_type IS
          '统计周期，固定为1m'
        """
    )
