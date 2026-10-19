"""Remove unused t_stat_record metadata and logical-delete columns.

Revision ID: 20261019_0012
Revises: 20261019_0011
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0012"
down_revision = "20261019_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_stat_record_lookup")
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP COLUMN IF EXISTS is_delete,
          DROP COLUMN IF EXISTS calc_ts,
          DROP COLUMN IF EXISTS create_time
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stat_record_lookup
          ON public.t_stat_record (sensor_no, channel, period_type, stat_time DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_stat_record_lookup")
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD COLUMN IF NOT EXISTS calc_ts TIMESTAMP(6),
          ADD COLUMN IF NOT EXISTS create_time TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP,
          ADD COLUMN IF NOT EXISTS is_delete BOOLEAN DEFAULT false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stat_record_lookup
          ON public.t_stat_record (sensor_no, channel, period_type, stat_time DESC)
          WHERE is_delete = false
        """
    )
