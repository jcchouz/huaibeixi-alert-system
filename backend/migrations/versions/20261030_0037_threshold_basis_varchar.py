"""将阈值设置依据字段调整为可变长字符串。

Revision ID: 20261030_0037
Revises: 20261030_0036
Create Date: 2026-10-30
"""

from alembic import op

revision = "20261030_0037"
down_revision = "20261030_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ALTER COLUMN threshold_basis TYPE VARCHAR(1000)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.threshold_basis IS
          '设置阈值等级和量程时采用的依据，最多1000个字符'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ALTER COLUMN threshold_basis TYPE TEXT
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.threshold_basis IS
          '设置阈值等级和量程时采用的依据，例如设计规范、历史统计或专家经验'
        """
    )
