"""Allow a handled alert to be followed by a new unhandled alert.

Revision ID: 20261020_0023
Revises: 20261020_0022
Create Date: 2026-10-20
"""

from alembic import op

revision = "20261020_0023"
down_revision = "20261020_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_active_identity")
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


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_alert_record_active_identity")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_record_active_identity
          ON public.t_alert_record (
              LOWER(sensor_no),
              COALESCE(channel, ''),
              alert_type,
              COALESCE(direction, '无方向')
          )
          WHERE alert_status IN (0, 1)
        """
    )
