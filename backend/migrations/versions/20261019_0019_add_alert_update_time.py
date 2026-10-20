"""Add last update time to alert records.

Revision ID: 20261019_0019
Revises: 20261019_0018
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261019_0019"
down_revision = "20261019_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS update_time TIMESTAMP(6)
          NOT NULL DEFAULT CURRENT_TIMESTAMP
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET update_time = COALESCE(ack_time, create_time, NOW())
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.create_time IS
          '告警首次触发时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.update_time IS
          '告警最近一次触发、升级或状态变更时间'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS update_time
        """
    )
