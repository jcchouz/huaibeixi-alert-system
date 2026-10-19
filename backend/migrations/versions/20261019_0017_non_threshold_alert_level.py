"""Set every non-threshold alert type to level three.

Revision ID: 20261019_0017
Revises: 20261019_0016
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0017"
down_revision = "20261019_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_level IS
          '告警等级：1一级红色，2二级橙色，3三级黄色'
        """
    )
