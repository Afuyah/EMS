"""attendance sessions for split shifts

Records every attendance session of a day, so someone who leaves and returns is
credited for both stints instead of only the first.

A day used to be one clock-in/clock-out pair. That silently discarded the
afternoon of anyone who went out mid-day, which is exactly the case a split
shift produces.

The existing ``clock_in`` / ``clock_out`` columns are left in place: they remain
the day's first arrival and latest departure, so every existing report, export
and query keeps working unchanged. ``worked_minutes`` becomes the sum across
sessions.

Nullable and defaulted to NULL, so existing rows need no backfill: a row with no
session list is read as a single session from the scalar columns.

Revision ID: 8b1f4c7a2d90
Revises: 3d055015212a
Create Date: 2026-10-16 10:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8b1f4c7a2d90'
down_revision = '3d055015212a'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'attendance_daily',
        sa.Column('sessions', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('attendance_daily', 'sessions')
