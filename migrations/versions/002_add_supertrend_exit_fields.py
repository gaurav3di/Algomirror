"""Add Supertrend exit fields to Strategy model

Revision ID: 002
Revises: 001
Create Date: 2025-10-15 13:16:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None

def upgrade():
    # Add Supertrend exit fields to strategy table
    with op.batch_alter_table('strategy', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supertrend_exit_enabled', sa.Boolean(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('supertrend_exit_type', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('supertrend_period', sa.Integer(), nullable=True, server_default='7'))
        batch_op.add_column(sa.Column('supertrend_multiplier', sa.Float(), nullable=True, server_default='3.0'))
        batch_op.add_column(sa.Column('supertrend_timeframe', sa.String(length=10), nullable=True, server_default='5m'))
        batch_op.add_column(sa.Column('supertrend_exit_triggered', sa.Boolean(), nullable=True, server_default='0'))

def downgrade():
    # Remove Supertrend exit fields from strategy table
    with op.batch_alter_table('strategy', schema=None) as batch_op:
        batch_op.drop_column('supertrend_exit_triggered')
        batch_op.drop_column('supertrend_timeframe')
        batch_op.drop_column('supertrend_multiplier')
        batch_op.drop_column('supertrend_period')
        batch_op.drop_column('supertrend_exit_type')
        batch_op.drop_column('supertrend_exit_enabled')
