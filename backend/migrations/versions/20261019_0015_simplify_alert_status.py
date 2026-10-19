"""Simplify alert status to unhandled or handled.

Revision ID: 20261019_0015
Revises: 20261019_0014
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0015"
down_revision = "20261019_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_status = 1,
            ack_by = COALESCE(ack_by, recover_by, 'system'),
            ack_time = COALESCE(ack_time, recover_time, NOW())
        WHERE alert_status = 2
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_status
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD CONSTRAINT ck_alert_record_status
          CHECK (alert_status IN (0, 1))
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS recover_by,
          DROP COLUMN IF EXISTS recover_time
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_status IS
          '告警状态：0未处理，1已处理'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS recover_by VARCHAR(64),
          ADD COLUMN IF NOT EXISTS recover_time TIMESTAMP(6)
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_status
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD CONSTRAINT ck_alert_record_status
          CHECK (alert_status IN (0, 1, 2))
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_status IS
          '告警状态：0未处理，1已确认，2已恢复'
        """
    )
