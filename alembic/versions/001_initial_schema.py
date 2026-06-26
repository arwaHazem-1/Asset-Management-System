"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-06-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    asset_type = postgresql.ENUM(
        "domain",
        "subdomain",
        "ip_address",
        "service",
        "certificate",
        "technology",
        name="asset_type",
        create_type=True,
    )
    asset_status = postgresql.ENUM(
        "active",
        "stale",
        "archived",
        name="asset_status",
        create_type=True,
    )
    asset_type.create(op.get_bind(), checkfirst=True)
    asset_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", asset_type, nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("status", asset_status, nullable=False, server_default="active"),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_assets_value"), "assets", ["value"], unique=False)
    op.create_index("ix_assets_type_value", "assets", ["type", "value"], unique=True)

    op.create_table(
        "asset_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("asset_relationships")
    op.drop_index("ix_assets_type_value", table_name="assets")
    op.drop_index(op.f("ix_assets_value"), table_name="assets")
    op.drop_table("assets")
    op.execute("DROP TYPE IF EXISTS asset_status")
    op.execute("DROP TYPE IF EXISTS asset_type")
