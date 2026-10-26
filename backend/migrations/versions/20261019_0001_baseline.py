"""Baseline alert, threshold and statistics schema.

This migration assumes the legacy monitoring tables already exist. It applies the
additive schema required by the application and can be stamped on an existing
database after verifying those changes are present.

Revision ID: 20261019_0001
Revises:
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP CONSTRAINT IF EXISTS fk_alert_threshold_sensor
        """
    )
    op.execute("ALTER TABLE public.t_alert_threshold ALTER COLUMN alert_level DROP NOT NULL")
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS channel VARCHAR(50) NOT NULL DEFAULT '',
          ADD COLUMN IF NOT EXISTS rule_type VARCHAR(30) NOT NULL DEFAULT 'THRESHOLD',
          ADD COLUMN IF NOT EXISTS monitor_enabled BOOLEAN NOT NULL DEFAULT true,
          ADD COLUMN IF NOT EXISTS expected_interval_ms INTEGER NOT NULL DEFAULT 1000,
          ADD COLUMN IF NOT EXISTS offline_timeout_ms INTEGER NOT NULL DEFAULT 10000,
          ADD COLUMN IF NOT EXISTS level1_upper NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS level1_lower NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS level2_upper NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS level2_lower NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS level3_upper NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS level3_lower NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS confirm_points INTEGER NOT NULL DEFAULT 3,
          ADD COLUMN IF NOT EXISTS recover_points INTEGER NOT NULL DEFAULT 3,
          ADD COLUMN IF NOT EXISTS hysteresis NUMERIC(15,5) NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS ext_params JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS update_by VARCHAR(64),
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
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS fk_alert_record_threshold,
          DROP CONSTRAINT IF EXISTS fk_alert_record_sensor
        """
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS public.seq_alert_record "
        "START WITH 1 INCREMENT BY 1"
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ALTER COLUMN threshold_id DROP NOT NULL,
          ALTER COLUMN trigger_val DROP NOT NULL,
          ALTER COLUMN channel SET DEFAULT '',
          ADD COLUMN IF NOT EXISTS seq BIGINT NOT NULL
            DEFAULT nextval('public.seq_alert_record'),
          ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(160),
          ADD COLUMN IF NOT EXISTS alert_type VARCHAR(30) NOT NULL DEFAULT 'THRESHOLD',
          ADD COLUMN IF NOT EXISTS alert_level SMALLINT NOT NULL DEFAULT 1,
          ADD COLUMN IF NOT EXISTS direction VARCHAR(10),
          ADD COLUMN IF NOT EXISTS peak_val NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS peak_time TIMESTAMP(6),
          ADD COLUMN IF NOT EXISTS last_fire_time TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP,
          ADD COLUMN IF NOT EXISTS trigger_count INTEGER NOT NULL DEFAULT 1,
          ADD COLUMN IF NOT EXISTS timeline_log JSONB NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN IF NOT EXISTS ack_by VARCHAR(64),
          ADD COLUMN IF NOT EXISTS ack_time TIMESTAMP(6),
          ADD COLUMN IF NOT EXISTS update_time TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_record_seq
          ON public.t_alert_record (seq)
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

    op.execute("ALTER TABLE public.t_stat_record SET (timescaledb.compress = false)")
    op.execute("UPDATE public.t_stat_record SET channel = '' WHERE channel IS NULL")
    op.execute(
        """
        ALTER TABLE public.t_stat_record
          ALTER COLUMN channel SET DEFAULT '',
          ALTER COLUMN channel SET NOT NULL,
          ADD COLUMN IF NOT EXISTS period_type VARCHAR(10) NOT NULL DEFAULT '1m',
          ADD COLUMN IF NOT EXISTS val_avg NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS val_std NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS max_time TIMESTAMP(6),
          ADD COLUMN IF NOT EXISTS min_time TIMESTAMP(6),
          ADD COLUMN IF NOT EXISTS sample_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS gap_count INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS ext_json JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_stat_record_bucket
          ON public.t_stat_record (stat_time, sensor_no, channel, period_type)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_stat_record_lookup
          ON public.t_stat_record (sensor_no, channel, period_type, stat_time DESC)
          WHERE is_delete = false
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "The baseline migration is intentionally irreversible because downgrade "
        "would delete production alert history columns."
    )
