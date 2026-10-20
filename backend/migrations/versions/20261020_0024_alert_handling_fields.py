"""Rename alert acknowledgement fields and add a handling opinion.

Revision ID: 20261020_0024
Revises: 20261020_0023
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0024"
down_revision = "20261020_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'ack_by'
          ) AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'handle_by'
          ) THEN
            ALTER TABLE public.t_alert_record
              RENAME COLUMN ack_by TO handle_by;
          END IF;

          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'ack_time'
          ) AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'handle_time'
          ) THEN
            ALTER TABLE public.t_alert_record
              RENAME COLUMN ack_time TO handle_time;
          END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS handle_opinion TEXT DEFAULT NULL
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_by IS
          '处理人'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_time IS
          '处理时间'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.handle_opinion IS
          '处理意见'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS handle_opinion
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'handle_by'
          ) AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'ack_by'
          ) THEN
            ALTER TABLE public.t_alert_record
              RENAME COLUMN handle_by TO ack_by;
          END IF;

          IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'handle_time'
          ) AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 't_alert_record'
              AND column_name = 'ack_time'
          ) THEN
            ALTER TABLE public.t_alert_record
              RENAME COLUMN handle_time TO ack_time;
          END IF;
        END
        $$;
        """
    )
