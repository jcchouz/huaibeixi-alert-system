"""Remove persistent detection state and worker watermark tables.

Revision ID: 20261023_0034
Revises: 20261022_0033
Create Date: 2026-10-23
"""

from alembic import op

revision = "20261023_0034"
down_revision = "20261022_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.t_detection_worker_state")
    op.execute("DROP TABLE IF EXISTS public.t_detection_channel_state")


def downgrade() -> None:
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
        CREATE TABLE IF NOT EXISTS public.t_detection_worker_state (
          worker_name VARCHAR(64) PRIMARY KEY,
          last_processed_time TIMESTAMP(6),
          last_completed_time TIMESTAMP(6),
          updated_at TIMESTAMP(6) NOT NULL DEFAULT LOCALTIMESTAMP
        )
        """
    )
