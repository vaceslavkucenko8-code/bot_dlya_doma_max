"""initial schema: buildings, users, incidents, reports, status_updates

Revision ID: 0001
Revises:
Create Date: 2026-09-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False на самом объекте типа: мы явно создаём тип один раз через
# .create(checkfirst=True) ниже и переиспользуем этот же объект в колонках,
# чтобы Alembic не пытался создать ENUM повторно при каждом CREATE TABLE,
# где он используется (иначе на второй таблице получаем "type already exists").
user_role_enum = postgresql.ENUM("resident", "management", name="user_role", create_type=False)
incident_status_enum = postgresql.ENUM("new", "in_progress", "resolved", name="incident_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    user_role_enum.create(bind, checkfirst=True)
    incident_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "buildings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.Column("management_company_external_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("address", name="uq_buildings_address"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_user_id", sa.String(length=100), nullable=False),
        sa.Column("role", user_role_enum, nullable=False, server_default="resident"),
        sa.Column("building_id", sa.Integer(), sa.ForeignKey("buildings.id"), nullable=True),
        sa.Column("entrance", sa.String(length=20), nullable=True),
        sa.Column("address_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("max_user_id", name="uq_users_max_user_id"),
    )
    op.create_index("ix_users_max_user_id", "users", ["max_user_id"])
    op.create_index("ix_users_building_id", "users", ["building_id"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("building_id", sa.Integer(), sa.ForeignKey("buildings.id"), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", incident_status_enum, nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_status_changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sla_last_reminded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Основной путь запроса при дедупликации: поиск открытых инцидентов
    # того же дома и той же категории. Порядок колонок важен: building_id
    # даёт наибольшую избирательность, дальше category и status.
    op.create_index(
        "ix_incidents_building_category_status",
        "incidents",
        ["building_id", "category", "status"],
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("photo_path", sa.String(length=500), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_reports_incident_id", "reports", ["incident_id"])
    op.create_index("ix_reports_user_id", "reports", ["user_id"])

    op.create_table(
        "status_updates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("incident_id", sa.Integer(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("status", incident_status_enum, nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False, server_default="management"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_status_updates_incident_id", "status_updates", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_status_updates_incident_id", table_name="status_updates")
    op.drop_table("status_updates")

    op.drop_index("ix_reports_user_id", table_name="reports")
    op.drop_index("ix_reports_incident_id", table_name="reports")
    op.drop_table("reports")

    op.drop_index("ix_incidents_building_category_status", table_name="incidents")
    op.drop_table("incidents")

    op.drop_index("ix_users_building_id", table_name="users")
    op.drop_index("ix_users_max_user_id", table_name="users")
    op.drop_table("users")

    op.drop_table("buildings")

    bind = op.get_bind()
    incident_status_enum.drop(bind, checkfirst=True)
    user_role_enum.drop(bind, checkfirst=True)
