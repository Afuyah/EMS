"""distinguish a device that is knocking from one that is working

A terminal with a mistyped CommKey reaches the server perfectly well while being
refused. Recording only the last *authenticated* contact makes it look like it
had never connected, which sends someone to check cables when the credential is
the problem.

``last_contact_at`` records any request claiming a known serial, successful or
not, so "is it reaching us?" and "is it authorised?" can be answered separately.

Nullable, so existing rows need no backfill: an unknown value simply means this
has not been observed since the upgrade.

Revision ID: c7d3e9a14f28
Revises: 8b1f4c7a2d90
Create Date: 2026-10-17 10:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d3e9a14f28'
down_revision = '8b1f4c7a2d90'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'biometric_devices',
        sa.Column('last_contact_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column('biometric_devices', 'last_contact_at')
