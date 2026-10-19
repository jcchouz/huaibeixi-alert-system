"""Remove the sampling-gap alert type and gap statistics.

Revision ID: 20261019_0010
Revises: 20261019_0009
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0010"
down_revision = "20261019_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM public.t_alert_record
        WHERE alert_type IN ('采样断流', 'GAP_1000MS')
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：阈值超限、传感器离线、采集仪离线、传感器死值、超物理量程'
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          DROP COLUMN IF EXISTS gap_count
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ADD COLUMN IF NOT EXISTS gap_count INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：阈值超限、传感器离线、采样断流、采集仪离线、传感器死值、超物理量程'
        """
    )
