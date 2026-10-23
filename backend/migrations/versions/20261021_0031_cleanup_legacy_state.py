"""Clean up legacy persisted channel state.

Revision ID: 20261021_0031
Revises: 20261021_0030
Create Date: 2026-10-22
"""

revision = "20261021_0031"
down_revision = "20261021_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The current engine ignores legacy runtime-only state fields.
    pass


def downgrade() -> None:
    pass
