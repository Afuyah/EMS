"""attendance policy lateness tiers and auto-absent cutoff

Revision ID: 60c1d0e9a9f7
Revises: 2954363fc854
Create Date: 2026-10-12 14:55:54.147325

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '60c1d0e9a9f7'
down_revision = '2954363fc854'
branch_labels = None
depends_on = None

#: Fallback very-late threshold, matching DEFAULT_VERY_LATE_AFTER_MINUTES.
DEFAULT_VERY_LATE_AFTER_MINUTES = 60


def upgrade():
    # ``very_late_after_minutes`` is NOT NULL, so it needs a server default to
    # backfill rows that already exist.  Without this the migration aborts on
    # any populated database (including production).
    with op.batch_alter_table('attendance_policies', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'very_late_after_minutes',
                sa.Integer(),
                nullable=False,
                server_default=str(DEFAULT_VERY_LATE_AFTER_MINUTES),
            )
        )
        # NULL means "no intra-day absence cut-off; the nightly job decides".
        batch_op.add_column(
            sa.Column('auto_absent_after_minutes', sa.Integer(), nullable=True)
        )

    # Drop the server default now that existing rows are backfilled: the
    # application supplies the value from here on, and leaving it would let the
    # database silently mask a missing value in future inserts.
    with op.batch_alter_table('attendance_policies', schema=None) as batch_op:
        batch_op.alter_column(
            'very_late_after_minutes', server_default=None
        )


def downgrade():
    with op.batch_alter_table('attendance_policies', schema=None) as batch_op:
        batch_op.drop_column('auto_absent_after_minutes')
        batch_op.drop_column('very_late_after_minutes')
