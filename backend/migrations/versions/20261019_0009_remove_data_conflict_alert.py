"""Remove data-conflict alert type.

Revision ID: 20261019_0009
Revises: 20261019_0008
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0009"
down_revision = "20261019_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM public.t_alert_record
        WHERE alert_type IN ('数据冲突', 'DATA_CONFLICT')
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：阈值超限、传感器离线、采样断流、采集仪离线、传感器死值、超物理量程'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：阈值超限、传感器离线、采样断流、采集仪离线、传感器死值、超物理量程、数据冲突'
        """
    )
