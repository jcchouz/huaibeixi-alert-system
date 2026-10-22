"""Track alert recovery time and remove trigger/direction metadata.

Revision ID: 20261022_0032
Revises: 20261021_0031
Create Date: 2026-10-22
"""

from alembic import op

revision = "20261022_0032"
down_revision = "20261021_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS public.ux_alert_record_active_identity"
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        RENAME COLUMN update_time TO end_time
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        ALTER COLUMN end_time DROP NOT NULL,
        ALTER COLUMN end_time DROP DEFAULT
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET end_time = NULL
        WHERE alert_status = 0
        """
    )
    op.execute(
        """
        WITH ranked_open AS (
          SELECT id,
                 ROW_NUMBER() OVER (
                   PARTITION BY LOWER(sensor_no), COALESCE(channel, ''), alert_type
                   ORDER BY seq DESC
                 ) AS row_rank
          FROM public.t_alert_record
          WHERE end_time IS NULL
        )
        UPDATE public.t_alert_record AS alert
        SET end_time = LOCALTIMESTAMP
        FROM ranked_open
        WHERE alert.id = ranked_open.id
          AND ranked_open.row_rank > 1
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        DROP COLUMN IF EXISTS trigger_count
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_alert_record_open_identity
        ON public.t_alert_record (
          LOWER(sensor_no),
          COALESCE(channel, ''),
          alert_type
        )
        WHERE end_time IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_alert_record_open_list
        ON public.t_alert_record (alert_level, create_time DESC)
        WHERE end_time IS NULL
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.create_time IS
          'Alert first trigger time'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.end_time IS
          'Alert condition recovery time; NULL means the incident is still open'
        """
    )
    op.execute(
        """
        UPDATE public.t_detection_channel_state
        SET state = state
          - 'threshold_streak_direction'
          - 'oor_streak_direction'
        WHERE state ? 'threshold_streak_direction'
           OR state ? 'oor_streak_direction'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_alert_record_open_list")
    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_open_identity")
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        ADD COLUMN trigger_count INTEGER NOT NULL DEFAULT 1
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET end_time = COALESCE(end_time, LOCALTIMESTAMP)
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        ALTER COLUMN end_time SET DEFAULT CURRENT_TIMESTAMP,
        ALTER COLUMN end_time SET NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
        RENAME COLUMN end_time TO update_time
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_status = 1
        WHERE alert_status = 0
          AND update_time IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_alert_record_active_identity
        ON public.t_alert_record (
          LOWER(sensor_no),
          COALESCE(channel, ''),
          alert_type
        )
        WHERE alert_status = 0
        """
    )
