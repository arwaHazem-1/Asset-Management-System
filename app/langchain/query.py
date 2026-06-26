import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Asset, AssetStatus
from app.schemas import AssetOut, QueryFilter
from app.utils import apply_tag_filter, apply_value_contains

logger = logging.getLogger(__name__)

# Every AI prompt includes this — the model only sees what we fetch from Postgres first.
GROUNDING_RULES = (
    "You are helping a security team review their external attack surface inventory. "
    "Only use the asset records provided below. Never invent hosts, certificates, or metadata. "
    "If the data does not support an answer, say so plainly."
)


def get_llm() -> ChatAnthropic:
    settings = get_settings()
    return ChatAnthropic(model="claude-sonnet-4-6", api_key=settings.anthropic_api_key or None)


def assets_to_context(assets: list[Asset]) -> str:
    payload = [
        {
            "id": str(a.id),
            "type": a.type.value,
            "value": a.value,
            "status": a.status.value,
            "tags": a.tags or [],
            "metadata": a.asset_metadata or {},
        }
        for a in assets
    ]
    return json.dumps(payload, indent=2, default=str)


async def parse_natural_language_query(question: str) -> QueryFilter:
    parser = PydanticOutputParser(pydantic_object=QueryFilter)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "Turn the user's plain-English question into filters for our asset database.\n\n"
                f"{GROUNDING_RULES}\n\n"
                "Available filter fields:\n"
                "- type (domain, subdomain, ip_address, service, certificate, technology)\n"
                "- status (active, stale, archived)\n"
                "- tags (e.g. prod, staging, dev)\n"
                "- value_contains (substring of the asset value)\n"
                "- metadata_conditions for cert dates: expires_before / expires_after as YYYY-MM-DD\n\n"
                "Set out_of_scope=true for questions unrelated to the inventory "
                "(weather, jokes, internal docs, etc.) and explain briefly in message.\n\n"
                "{format_instructions}",
            ),
            ("human", "{question}"),
        ]
    )
    chain = prompt | get_llm() | parser
    return await chain.ainvoke(
        {"question": question, "format_instructions": parser.get_format_instructions()}
    )


def _parse_date(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value.replace("+00:00", "Z"), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _metadata_matches(asset_meta: dict, conditions: dict[str, Any]) -> bool:
    if not conditions:
        return True
    meta = asset_meta or {}
    for key, expected in conditions.items():
        if key == "expires_before":
            expires = meta.get("expires") or meta.get("expires_at")
            if not expires:
                return False
            dt = _parse_date(str(expires))
            threshold = _parse_date(str(expected))
            if not dt or not threshold or dt >= threshold:
                return False
        elif key == "expires_after":
            expires = meta.get("expires") or meta.get("expires_at")
            if not expires:
                return False
            dt = _parse_date(str(expires))
            threshold = _parse_date(str(expected))
            if not dt or not threshold or dt <= threshold:
                return False
        elif key == "contains":
            if not isinstance(expected, dict):
                return False
            for sub_key, sub_val in expected.items():
                if meta.get(sub_key) != sub_val:
                    return False
        else:
            if meta.get(key) != expected:
                return False
    return True


async def execute_query_filter(db: AsyncSession, filters: QueryFilter) -> list[Asset]:
    query = select(Asset).where(Asset.status != AssetStatus.archived)

    if filters.type:
        query = query.where(Asset.type == filters.type)
    if filters.status:
        query = query.where(Asset.status == filters.status)
    if filters.value_contains:
        query = apply_value_contains(query, filters.value_contains)
    for tag in filters.tags:
        query = await apply_tag_filter(query, tag, db)

    result = await db.execute(query)
    assets = list(result.scalars().all())

    if filters.metadata_conditions:
        assets = [a for a in assets if _metadata_matches(a.asset_metadata, filters.metadata_conditions)]

    return assets


async def run_natural_language_query(
    db: AsyncSession, question: str
) -> tuple[list[AssetOut], str | None, QueryFilter]:
    filters = await parse_natural_language_query(question)

    if filters.out_of_scope:
        return [], filters.message or "That question isn't about our asset inventory.", filters

    assets = await execute_query_filter(db, filters)
    if not assets:
        return [], "Nothing in the database matched those filters.", filters

    return [AssetOut.from_orm_asset(a) for a in assets], None, filters
