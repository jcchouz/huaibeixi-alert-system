"""Persist detection state, worker watermarks and threshold rule versions.

Revision ID: 20261021_0030
Revises: 20261020_0028
Create Date: 2026-10-21
"""

from alembic import op

revision = "20261021_0030"
down_revision = "20261020_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.t_detection_channel_state (
          sensor_no VARCHAR(64) NOT NULL,
          channel VARCHAR(50) NOT NULL DEFAULT '',
          state JSONB NOT NULL DEFAULT '{}'::jsonb,
          updated_at TIMESTAMP(6) NOT NULL DEFAULT LOCALTIMESTAMP,
          PRIMARY KEY (sensor_no, channel)
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE public.t_detection_channel_state IS
          '持久化通道连续命中、在线状态和活动条件状态'
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.t_detection_worker_state (
          worker_name VARCHAR(64) PRIMARY KEY,
          last_processed_time TIMESTAMP(6),
          last_completed_time TIMESTAMP(6),
          updated_at TIMESTAMP(6) NOT NULL DEFAULT LOCALTIMESTAMP
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE public.t_detection_worker_state IS
          '实时预警和统计任务持久化水位，用于重启和主备切换后的续跑'
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.t_detection_rule_version (
          id SMALLINT PRIMARY KEY DEFAULT 1,
          version BIGINT NOT NULL DEFAULT 1,
          updated_at TIMESTAMP(6) NOT NULL DEFAULT LOCALTIMESTAMP,
          CONSTRAINT ck_detection_rule_version_singleton CHECK (id = 1)
        )
        """
    )
    op.execute(
        """
        INSERT INTO public.t_detection_rule_version (id, version)
        VALUES (1, 1)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.bump_detection_rule_version()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
          UPDATE public.t_detection_rule_version
          SET version = version + 1,
              updated_at = LOCALTIMESTAMP
          WHERE id = 1;
          RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_detection_rule_version
          ON public.t_alert_threshold
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_detection_rule_version
        AFTER INSERT OR UPDATE OR DELETE ON public.t_alert_threshold
        FOR EACH STATEMENT
        EXECUTE FUNCTION public.bump_detection_rule_version()
        """
    )
    op.execute(
        """
        COMMENT ON TABLE public.t_detection_rule_version IS
          '阈值规则版本；t_alert_threshold 发生语句级写入时自动递增'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_detection_rule_version
          ON public.t_alert_threshold
        """
    )
    op.execute("DROP FUNCTION IF EXISTS public.bump_detection_rule_version()")
    op.execute("DROP TABLE IF EXISTS public.t_detection_rule_version")
    op.execute("DROP TABLE IF EXISTS public.t_detection_worker_state")
    op.execute("DROP TABLE IF EXISTS public.t_detection_channel_state")
