"""leave recall fields and reschedule link

Revision ID: 7ee784794796
Revises: 60c1d0e9a9f7
Create Date: 2026-10-12 16:27:12.453756

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7ee784794796'
down_revision = '60c1d0e9a9f7'
branch_labels = None
depends_on = None

#: Named explicitly: Alembic leaves the constraint unnamed otherwise, which
#: makes downgrade() fail because it has nothing to drop by name.
FK_NAME = "fk_leave_requests_rescheduled_to"


def upgrade():
    # Both columns are nullable, so existing rows need no backfill.
    with op.batch_alter_table('leave_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('recall_reason', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('rescheduled_to_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            FK_NAME,
            'leave_requests',
            ['rescheduled_to_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('leave_requests', schema=None) as batch_op:
        batch_op.drop_constraint(FK_NAME, type_='foreignkey')
        batch_op.drop_column('rescheduled_to_id')
        batch_op.drop_column('recall_reason')
