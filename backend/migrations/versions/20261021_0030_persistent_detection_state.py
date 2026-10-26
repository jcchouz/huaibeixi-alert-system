"""Persist detection state and worker watermarks.

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


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.t_detection_worker_state")
    op.execute("DROP TABLE IF EXISTS public.t_detection_channel_state")
