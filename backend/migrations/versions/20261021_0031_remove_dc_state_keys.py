"""Remove obsolete DC offset keys from persisted channel state.

Revision ID: 20261021_0031
Revises: 20261021_0030
Create Date: 2026-10-22
"""

from alembic import op

revision = "20261021_0031"
down_revision = "20261021_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.t_detection_channel_state
        SET state = state - 'realtime_dc_offset'
        WHERE state ? 'realtime_dc_offset'
        """
    )


def downgrade() -> None:
    # Removed values cannot be reconstructed; this is an intentional data cleanup.
    pass
