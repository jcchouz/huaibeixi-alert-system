"""Store alert types in Chinese.

Revision ID: 20261019_0008
Revises: 20261019_0007
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0008"
down_revision = "20261019_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_type = CASE alert_type
            WHEN 'THRESHOLD' THEN '阈值超限'
            WHEN 'OFFLINE' THEN '传感器离线'
            WHEN 'GAP_1000MS' THEN '采样断流'
            WHEN 'DAQ_OFFLINE' THEN '采集仪离线'
            WHEN 'STUCK' THEN '传感器死值'
            WHEN 'OOR' THEN '超物理量程'
            ELSE alert_type
        END
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ALTER COLUMN alert_type SET DEFAULT '阈值超限'
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
        UPDATE public.t_alert_record
        SET alert_type = CASE alert_type
            WHEN '阈值超限' THEN 'THRESHOLD'
            WHEN '传感器离线' THEN 'OFFLINE'
            WHEN '采样断流' THEN 'GAP_1000MS'
            WHEN '采集仪离线' THEN 'DAQ_OFFLINE'
            WHEN '传感器死值' THEN 'STUCK'
            WHEN '超物理量程' THEN 'OOR'
            ELSE alert_type
        END
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ALTER COLUMN alert_type SET DEFAULT 'THRESHOLD'
        """
    )
