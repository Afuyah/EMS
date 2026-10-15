"""flag users who must change their password

Revision ID: 32cf4a35352b
Revises: 3116811a14651
Create Date: 2026-10-12 17:57:34.533966

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '32cf4a35352b'
down_revision = '311681a14651'
branch_labels = None
depends_on = None


def upgrade():
    # ``must_change_password`` is NOT NULL, so it needs a server default to
    # backfill rows that already exist.  Without one this aborts on any
    # populated database.
    #
    # Existing users chose their own passwords (or are seeded demo accounts), so
    # False is the correct backfill — forcing a change on everyone would lock
    # the current team out of a working system for no security gain.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'must_change_password',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # Drop the default now that existing rows are backfilled: the application
    # supplies the value from here on, and leaving it would let the database
    # mask a missing value in future inserts.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('must_change_password', server_default=None)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('must_change_password')
