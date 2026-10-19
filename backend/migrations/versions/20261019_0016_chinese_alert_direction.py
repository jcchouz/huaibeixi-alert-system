"""Store alert direction values in Chinese.

Revision ID: 20261019_0016
Revises: 20261019_0015
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0016"
down_revision = "20261019_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record
        SET direction = CASE UPPER(COALESCE(direction, ''))
            WHEN 'UPPER' THEN '上限超限'
            WHEN 'LOWER' THEN '下限超限'
            WHEN 'NONE' THEN '无方向'
            WHEN '' THEN '无方向'
            WHEN '上限超限' THEN '上限超限'
            WHEN '下限超限' THEN '下限超限'
            WHEN '无方向' THEN '无方向'
            ELSE '无方向'
        END
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ALTER COLUMN direction SET DEFAULT '无方向',
          ALTER COLUMN direction SET NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_direction
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ADD CONSTRAINT ck_alert_record_direction
          CHECK (direction IN ('上限超限', '下限超限', '无方向'))
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.direction IS
          '告警方向：上限超限、下限超限、无方向'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          DROP CONSTRAINT IF EXISTS ck_alert_record_direction
        """
    )
    op.execute(
        """
        UPDATE public.t_alert_record
        SET direction = CASE direction
            WHEN '上限超限' THEN 'UPPER'
            WHEN '下限超限' THEN 'LOWER'
            ELSE 'NONE'
        END
        """
    )
    op.execute(
        """
        ALTER TABLE public.t_alert_record
          ALTER COLUMN direction SET DEFAULT 'NONE'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_record.direction IS
          '越限方向：UPPER超上限，LOWER超下限，NONE无方向'
        """
    )
