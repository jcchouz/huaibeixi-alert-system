"""Keep only one-minute records in t_stat_record.

Revision ID: 20261020_0020
Revises: 20261019_0019
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0020"
down_revision = "20261019_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM public.t_stat_record
        WHERE period_type <> '1m'
        """
    )
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


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP CONSTRAINT IF EXISTS ck_stat_record_period_type
        """
    )
