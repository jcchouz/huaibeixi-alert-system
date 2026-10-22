"""Drop the redundant threshold sampling interval.

Revision ID: 20261022_0033
Revises: 20261022_0032
Create Date: 2026-10-22
"""

from alembic import op

revision = "20261022_0033"
down_revision = "20261022_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
        DROP COLUMN IF EXISTS expected_interval_ms
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
        ADD COLUMN IF NOT EXISTS expected_interval_ms INTEGER
        NOT NULL DEFAULT 1000
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_threshold AS threshold
        SET expected_interval_ms = GREATEST(
          1,
          ROUND(1000.0 / COALESCE(
            NULLIF(
              CASE
                WHEN POSITION('/' IN COALESCE(sensor.frequency, '')) > 0
                THEN SPLIT_PART(sensor.frequency, '/', 1)::DOUBLE PRECISION
                     / NULLIF(SPLIT_PART(sensor.frequency, '/', 2)::DOUBLE PRECISION, 0)
                ELSE NULLIF(sensor.frequency, '')::DOUBLE PRECISION
              END,
              0
            ),
            1
          ))
        )::INTEGER
        FROM public.t_sensor AS sensor
        WHERE LOWER(sensor.sensor_no) = LOWER(threshold.sensor_no)
        """
    )
