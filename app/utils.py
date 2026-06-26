"""Small helpers shared across routers and the LangChain layer."""

from sqlalchemy import String, cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset


async def apply_tag_filter(query, tag: str, db: AsyncSession):
    """PostgreSQL uses native array containment; SQLite tests match inside JSON."""
    conn = await db.connection()
    if conn.dialect.name == "postgresql":
        return query.where(Asset.tags.contains([tag]))
    return query.where(cast(Asset.tags, String).like(f'%"{tag}"%'))


def apply_value_contains(query, needle: str):
    return query.where(Asset.value.ilike(f"%{needle}%"))
