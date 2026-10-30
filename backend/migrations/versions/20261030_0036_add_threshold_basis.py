"""为阈值规则增加设置依据字段。

Revision ID: 20261030_0036
Revises: 20261026_0035
Create Date: 2026-10-30
"""

from alembic import op

revision = "20261030_0036"
down_revision = "20261026_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS threshold_basis TEXT
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.threshold_basis IS
          '设置阈值等级和量程时采用的依据，例如设计规范、历史统计或专家经验'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP COLUMN IF EXISTS threshold_basis
        """
    )
