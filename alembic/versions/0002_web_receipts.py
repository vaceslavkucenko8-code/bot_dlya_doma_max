"""Idempotency receipts for the web adapter."""
from alembic import op
import sqlalchemy as sa
revision='0002_web_receipts'
down_revision='0001'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('web_receipts',sa.Column('request_id',sa.String(36),primary_key=True),sa.Column('incident_id',sa.Integer(),sa.ForeignKey('incidents.id'),nullable=False),sa.Column('fingerprint',sa.String(64),nullable=False))

def downgrade():
    op.drop_table('web_receipts')
