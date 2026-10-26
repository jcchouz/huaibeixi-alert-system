"""Simplify threshold rules and keep recovery manual.

Revision ID: 20261019_0005
Revises: 20261019_0004
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0005"
down_revision = "20261019_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS create_by VARCHAR(64) DEFAULT ''
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_threshold
        SET create_by = 'system'
        WHERE create_by IS NULL OR create_by = ''
        """
    )
    op.execute("DROP INDEX IF EXISTS public.uk_alert_threshold_target")
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP COLUMN IF EXISTS is_delete,
          DROP COLUMN IF EXISTS rule_type,
          DROP COLUMN IF EXISTS monitor_enabled,
          DROP COLUMN IF EXISTS recover_points,
          DROP COLUMN IF EXISTS hysteresis,
          DROP COLUMN IF EXISTS ext_params,
          DROP COLUMN IF EXISTS version,
          DROP COLUMN IF EXISTS change_reason,
          DROP COLUMN IF EXISTS history_log
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_alert_threshold_target
          ON public.t_alert_threshold (LOWER(sensor_no), channel)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.create_by IS
          '阈值规则创建人'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.confirm_points IS
          '连续超限确认点数，达到该点数后触发告警'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.expected_interval_ms IS
          '期望采样间隔，单位毫秒'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.uk_alert_threshold_target")
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS is_delete BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS rule_type VARCHAR(30) NOT NULL DEFAULT 'THRESHOLD',
          ADD COLUMN IF NOT EXISTS monitor_enabled BOOLEAN NOT NULL DEFAULT true,
          ADD COLUMN IF NOT EXISTS recover_points INTEGER NOT NULL DEFAULT 3,
          ADD COLUMN IF NOT EXISTS hysteresis NUMERIC(15,5) NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS ext_params JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS change_reason VARCHAR(255)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uk_alert_threshold_target
          ON public.t_alert_threshold (LOWER(sensor_no), channel, rule_type)
          WHERE is_delete = false
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP COLUMN IF EXISTS create_by
        """
    )
