"""Remove threshold rule versions and store one alert rule snapshot.

Revision ID: 20261026_0035
Revises: 20261023_0034
Create Date: 2026-10-26
"""

from alembic import op

revision = "20261026_0035"
down_revision = "20261023_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_detection_rule_version
          ON public.t_alert_threshold
        """
    )
    op.execute("DROP FUNCTION IF EXISTS public.bump_detection_rule_version()")
    op.execute("DROP TABLE IF EXISTS public.t_detection_rule_version")
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
        DROP COLUMN IF EXISTS version,
        DROP COLUMN IF EXISTS history_log
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        ADD COLUMN IF NOT EXISTS rule_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.rule_snapshot IS
          '预警首次创建时保存的规则内容；活动预警后续更新不再修改'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        DROP COLUMN IF EXISTS rule_snapshot
        """
    )
