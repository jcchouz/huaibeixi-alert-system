"""Remove rate-of-change and spike monitoring.

Revision ID: 20261019_0003
Revises: 20261019_0002
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0003"
down_revision = "20261019_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM public.t_alert_record
        WHERE UPPER(COALESCE(alert_type, '')) IN ('RATE', 'SPIKE')
        """
    )
    op.execute(
        """
        DELETE FROM public.t_alert_threshold
        WHERE UPPER(COALESCE(rule_type, '')) IN ('RATE', 'SPIKE')
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：THRESHOLD、OFFLINE、GAP_1000MS、STUCK、OOR、DAQ_OFFLINE'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.rule_type IS
          '规则类型：THRESHOLD阈值、OFFLINE离线、STUCK死值、OOR超量程'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.ext_params IS
          '算法扩展参数JSON，例如物理量程和严格采样间隔'
        """
    )


def downgrade() -> None:
    # Deleted alert and rule rows cannot be reconstructed.
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_type IS
          '告警类型：THRESHOLD、OFFLINE、GAP_1000MS、RATE、STUCK、OOR、SPIKE、DAQ_OFFLINE'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.rule_type IS
          '规则类型：THRESHOLD阈值、OFFLINE离线、RATE变化率、STUCK死值、OOR超量程、SPIKE毛刺'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.ext_params IS
          '算法扩展参数JSON，例如物理量程、严格采样间隔和变化率参数'
        """
    )
