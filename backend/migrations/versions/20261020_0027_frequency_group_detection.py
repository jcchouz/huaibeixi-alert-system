"""Switch detection configuration to frequency-based sample groups.

Revision ID: 20261020_0027
Revises: 20261020_0026
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0027"
down_revision = "20261020_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_active_identity")
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_direction
        """
    )

    # Direction is no longer part of an active incident identity. Keep only the
    # latest unhandled row when legacy upper/lower alerts collapse into one key.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY LOWER(sensor_no),
                                    COALESCE(channel, ''),
                                    alert_type
                       ORDER BY seq DESC
                   ) AS rn
            FROM public.t_alert_record
            WHERE alert_status = 0
        )
        UPDATE public.t_alert_record AS a
        SET alert_status = 1,
            handle_by = COALESCE(a.handle_by, 'system'),
            handle_time = COALESCE(a.handle_time, NOW()),
            update_time = NOW()
        FROM ranked AS r
        WHERE a.id = r.id AND r.rn > 1
        """
    )

    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS remark,
          DROP COLUMN IF EXISTS direction,
          ADD COLUMN IF NOT EXISTS handle_phone VARCHAR(32)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_record_active_identity
          ON public.t_alert_record (
              LOWER(sensor_no),
              COALESCE(channel, ''),
              alert_type
          )
          WHERE alert_status = 0
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_phone IS
          'Handler phone number'
        """
    )

    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP COLUMN IF EXISTS offline_timeout_ms,
          DROP COLUMN IF EXISTS stuck_time_ms,
          ADD COLUMN IF NOT EXISTS stuck_points INTEGER NOT NULL DEFAULT 100,
          ADD COLUMN IF NOT EXISTS offline_points INTEGER NOT NULL DEFAULT 5,
          ALTER COLUMN confirm_points SET DEFAULT 3
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_threshold
        SET confirm_points = 3
        WHERE confirm_points IS DISTINCT FROM 3
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_confirm_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_stuck_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_offline_points,
          ADD CONSTRAINT ck_alert_threshold_confirm_points
            CHECK (confirm_points > 0),
          ADD CONSTRAINT ck_alert_threshold_stuck_points
            CHECK (stuck_points > 0),
          ADD CONSTRAINT ck_alert_threshold_offline_points
            CHECK (offline_points > 0)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.confirm_points IS
          'Consecutive samples for threshold and out-of-range alerts'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.stuck_points IS
          'Consecutive equal samples for stuck sensor alerts'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.offline_points IS
          'Consecutive missing sample periods for offline alerts'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_confirm_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_stuck_points,
          DROP CONSTRAINT IF EXISTS ck_alert_threshold_offline_points,
          DROP COLUMN IF EXISTS stuck_points,
          DROP COLUMN IF EXISTS offline_points,
          ADD COLUMN IF NOT EXISTS offline_timeout_ms INTEGER NOT NULL DEFAULT 10000,
          ADD COLUMN IF NOT EXISTS stuck_time_ms INTEGER NOT NULL DEFAULT 30000
        """
    )

    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_active_identity")
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS handle_phone,
          ADD COLUMN IF NOT EXISTS remark TEXT DEFAULT '',
          ADD COLUMN IF NOT EXISTS direction VARCHAR(10) NOT NULL DEFAULT '无方向'
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_direction,
          ADD CONSTRAINT ck_alert_record_direction
            CHECK (direction IN ('上限超限', '下限超限', '无方向'))
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_record_active_identity
          ON public.t_alert_record (
              LOWER(sensor_no),
              COALESCE(channel, ''),
              alert_type,
              COALESCE(direction, '无方向')
          )
          WHERE alert_status = 0
        """
    )
