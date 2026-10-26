"""Simplify alert records and keep recovery manual.

Revision ID: 20261019_0006
Revises: 20261019_0005
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0006"
down_revision = "20261019_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_active_dedup")
    op.execute("DROP INDEX IF EXISTS public.idx_alert_record_list")

    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS recover_by VARCHAR(64),
          ADD COLUMN IF NOT EXISTS recover_time TIMESTAMP(6)
        """
    )

    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_status = 2,
            end_ts = COALESCE(end_ts, ack_time, create_time, NOW()),
            recover_by = COALESCE(recover_by, ack_by, 'system'),
            recover_time = COALESCE(recover_time, end_ts, ack_time, create_time, NOW())
        WHERE alert_status IN (3, 4)
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_status = 0
        WHERE alert_status IS NULL
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET end_ts = COALESCE(end_ts, ack_time, create_time, NOW()),
            recover_by = COALESCE(recover_by, ack_by, 'system'),
            recover_time = COALESCE(recover_time, end_ts, ack_time, create_time, NOW())
        WHERE alert_status = 2
        """
    )

    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY LOWER(sensor_no),
                                    COALESCE(channel, ''),
                                    alert_type,
                                    COALESCE(direction, 'NONE')
                       ORDER BY seq DESC
                   ) AS rn
            FROM public.t_alert_record
            WHERE alert_status IN (0, 1)
        )
        UPDATE public.t_alert_record AS a
        SET alert_status = 2,
            end_ts = COALESCE(a.end_ts, a.create_time, NOW()),
            recover_by = COALESCE(a.recover_by, a.ack_by, 'system'),
            recover_time = COALESCE(a.recover_time, a.end_ts, a.create_time, NOW())
        FROM ranked AS r
        WHERE a.id = r.id AND r.rn > 1
        """
    )

    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ALTER COLUMN alert_status SET DEFAULT 0,
          ALTER COLUMN alert_status SET NOT NULL,
          DROP COLUMN IF EXISTS is_delete,
          DROP COLUMN IF EXISTS dedup_key,
          DROP COLUMN IF EXISTS peak_val,
          DROP COLUMN IF EXISTS peak_time,
          DROP COLUMN IF EXISTS last_fire_time,
          DROP COLUMN IF EXISTS rule_version,
          DROP COLUMN IF EXISTS threshold_snapshot,
          DROP COLUMN IF EXISTS timeline_log,
          DROP COLUMN IF EXISTS update_time
        """
    )

    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_status,
          ADD CONSTRAINT ck_alert_record_status
            CHECK (alert_status IN (0, 1, 2))
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_record_active_identity
          ON public.t_alert_record (
              LOWER(sensor_no),
              COALESCE(channel, ''),
              alert_type,
              COALESCE(direction, 'NONE')
          )
          WHERE alert_status IN (0, 1)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alert_record_list
          ON public.t_alert_record (alert_status, alert_level DESC, create_time DESC)
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_status IS
          '告警状态：0未处理，1已确认，2已恢复';
        COMMENT ON COLUMN public.t_alert_record.recover_by IS
          '人工恢复人';
        COMMENT ON COLUMN public.t_alert_record.recover_time IS
          '人工恢复时间';
        COMMENT ON COLUMN public.t_alert_record.end_ts IS
          '告警结束时间，人工恢复时写入';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_active_identity")
    op.execute("DROP INDEX IF EXISTS public.idx_alert_record_list")
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_status
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS is_delete BOOLEAN DEFAULT false,
          ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(160),
          ADD COLUMN IF NOT EXISTS peak_val NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS peak_time TIMESTAMP(6),
          ADD COLUMN IF NOT EXISTS last_fire_time TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP,
          ADD COLUMN IF NOT EXISTS timeline_log JSONB NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN IF NOT EXISTS update_time TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_record_active_dedup
          ON public.t_alert_record (dedup_key)
          WHERE alert_status IN (0, 1, 4) AND is_delete = false
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alert_record_list
          ON public.t_alert_record (alert_status, alert_level DESC, create_time DESC)
          WHERE is_delete = false
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS recover_by,
          DROP COLUMN IF EXISTS recover_time
        """
    )
