"""Set direction for out-of-physical-range alerts.

Revision ID: 20261019_0018
Revises: 20261019_0017
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0018"
down_revision = "20261019_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record AS a
        SET direction = CASE
            WHEN t.range_min IS NOT NULL
             AND a.trigger_val < t.range_min THEN '下限超限'
            WHEN t.range_max IS NOT NULL
             AND a.trigger_val > t.range_max THEN '上限超限'
            ELSE a.direction
        END
        FROM public.t_alert_threshold AS t
        WHERE a.alert_type = '超物理量程'
          AND LOWER(t.sensor_no) = LOWER(a.sensor_no)
          AND COALESCE(t.channel, '') = COALESCE(a.channel, '')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_record AS a
        SET direction = '无方向'
        FROM public.t_alert_threshold AS t
        WHERE a.alert_type = '超物理量程'
          AND LOWER(t.sensor_no) = LOWER(a.sensor_no)
          AND COALESCE(t.channel, '') = COALESCE(a.channel, '')
        """
    )
