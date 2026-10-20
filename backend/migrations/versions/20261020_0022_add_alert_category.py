"""Add alert category to alert records.

Revision ID: 20261020_0022
Revises: 20261020_0021
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0022"
down_revision = "20261020_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD COLUMN IF NOT EXISTS alert_category SMALLINT
          NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET alert_category = CASE
            WHEN alert_type IN ('传感器离线', '采集仪离线') THEN 1
            ELSE 0
        END
        WHERE alert_category IS DISTINCT FROM CASE
            WHEN alert_type IN ('传感器离线', '采集仪离线') THEN 1
            ELSE 0
        END
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_alert_category
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD CONSTRAINT ck_alert_record_alert_category
          CHECK (alert_category IN (0, 1))
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.alert_category IS
          '预警分类：0=数据预警，1=设备预警'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_alert_category
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP COLUMN IF EXISTS alert_category
        """
    )
