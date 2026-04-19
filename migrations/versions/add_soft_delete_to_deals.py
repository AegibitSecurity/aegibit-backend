"""Add soft delete fields to deals table

Revision ID: add_soft_delete_deals
Revises:
Create Date: 2026-04-16

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_soft_delete_deals'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Add is_deleted column with default False
    op.add_column('deals', sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='0'))
    
    # Add deleted_at column (nullable)
    op.add_column('deals', sa.Column('deleted_at', sa.DateTime(), nullable=True))
    
    # Add deleted_by column (nullable, FK to users)
    op.add_column('deals', sa.Column('deleted_by', sa.String(36), nullable=True))
    
    # Create index on is_deleted for fast filtering
    op.create_index('ix_deal_is_deleted', 'deals', ['is_deleted'])
    
    # Create FK constraint for deleted_by (optional - if you want to enforce referential integrity)
    # op.create_foreign_key('fk_deals_deleted_by_users', 'deals', 'users', ['deleted_by'], ['id'])


def downgrade():
    # Remove the columns and index
    op.drop_index('ix_deal_is_deleted', table_name='deals')
    op.drop_column('deals', 'deleted_by')
    op.drop_column('deals', 'deleted_at')
    op.drop_column('deals', 'is_deleted')
