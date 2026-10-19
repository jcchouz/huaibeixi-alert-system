"""Add a UUID-style id to t_stat_record.

Revision ID: 20261019_0014
Revises: 20261019_0013
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0014"
down_revision = "20261019_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD COLUMN IF NOT EXISTS id VARCHAR(32)
        """
    )
    op.execute(
        """
        UPDATE public.t_stat_record
        SET id = SUBSTRING(
            MD5(
                stat_time::TEXT
                || sensor_no
                || COALESCE(channel, '')
                || period_type
                || RANDOM()::TEXT
            ),
            1,
            32
        )
        WHERE id IS NULL OR id = ''
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ALTER COLUMN id SET NOT NULL,
          ALTER COLUMN id SET DEFAULT
            SUBSTRING(MD5(CLOCK_TIMESTAMP()::TEXT || RANDOM()::TEXT), 1, 32)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stat_record_id
          ON public.t_stat_record (id)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_stat_record.id IS
          '统计记录ID，32位十六进制字符串'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_stat_record_id")
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP COLUMN IF EXISTS id
        """
    )
