"""Remove retired monitoring parameters from threshold rules.

Revision ID: 20261019_0004
Revises: 20261019_0003
Create Date: 2026-10-19
"""

from alembic import op

revision = "20261019_0004"
down_revision = "20261019_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_alert_threshold
        SET ext_params = ext_params
          - 'max_rate_per_sec'
          - 'rate_limit'
          - 'spike_sigma'
          - 'spike_ratio'
        WHERE ext_params ?| ARRAY[
          'max_rate_per_sec',
          'rate_limit',
          'spike_sigma',
          'spike_ratio'
        ]
        """
    )


def downgrade() -> None:
    pass
