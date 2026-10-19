"""Remove alert end timestamp and keep remark empty.

Revision ID: 20261019_0007
Revises: 20261019_0006
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0007"
down_revision = "20261019_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS end_ts,
          ALTER COLUMN remark SET DEFAULT ''
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET remark = ''
        WHERE remark IS DISTINCT FROM ''
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.remark IS
          '告警备注，默认空字符串，系统不写入'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS end_ts TIMESTAMP(6),
          ALTER COLUMN remark DROP DEFAULT
        """
    )
