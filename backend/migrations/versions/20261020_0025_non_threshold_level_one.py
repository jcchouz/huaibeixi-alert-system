"""Set non-threshold alerts to level one and normalize offline trigger values.

Revision ID: 20261020_0025
Revises: 20261020_0024
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0025"
down_revision = "20261020_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_level = 1
        WHERE alert_type <> '阈值超限'
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET trigger_val = 0
        WHERE alert_type IN ('传感器离线', '采集仪离线')
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_level IS
          '告警等级：阈值超限支持1/2/3级；其余告警类型固定为1级'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_level = 3
        WHERE alert_type <> '阈值超限'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_level IS
          '告警等级：阈值超限支持1/2/3级；其余告警类型固定为3级'
        """
    )
