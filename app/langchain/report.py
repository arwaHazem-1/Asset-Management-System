from datetime import datetime, timezone
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.langchain.query import GROUNDING_RULES, assets_to_context, get_llm
from app.models import Asset, AssetStatus, AssetType
from app.utils import apply_tag_filter, apply_value_contains


def _parse_expiry(meta: dict) -> datetime | None:
    raw = meta.get("expires") or meta.get("expires_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


async def fetch_assets_for_report(
    db: AsyncSession, filters: dict[str, Any] | None = None
) -> list[Asset]:
    query = select(Asset).where(Asset.status != AssetStatus.archived)

    if filters:
        if filters.get("type"):
            query = query.where(Asset.type == AssetType(filters["type"]))
        if filters.get("status"):
            query = query.where(Asset.status == AssetStatus(filters["status"]))
        if filters.get("tag"):
            query = await apply_tag_filter(query, filters["tag"], db)
        if filters.get("value_contains"):
            query = apply_value_contains(query, filters["value_contains"])

    result = await db.execute(query)
    return list(result.scalars().all())


def compute_inventory_stats(assets: list[Asset]) -> dict[str, Any]:
    """Counts are calculated here — the LLM writes prose, not arithmetic."""
    now = datetime.now(timezone.utc)
    stats: dict[str, Any] = {
        "total_assets": len(assets),
        "by_type": {},
        "by_status": {},
        "expired_certificates": 0,
        "expiring_within_30_days": 0,
        "sensitive_exposed_services": 0,
        "eol_technologies": 0,
        "tags_summary": {},
    }

    for asset in assets:
        stats["by_type"][asset.type.value] = stats["by_type"].get(asset.type.value, 0) + 1
        stats["by_status"][asset.status.value] = stats["by_status"].get(asset.status.value, 0) + 1
        for tag in asset.tags or []:
            stats["tags_summary"][tag] = stats["tags_summary"].get(tag, 0) + 1

        if asset.type == AssetType.certificate:
            expires = _parse_expiry(asset.asset_metadata or {})
            if expires:
                if expires < now:
                    stats["expired_certificates"] += 1
                elif (expires - now).days <= 30:
                    stats["expiring_within_30_days"] += 1

        if asset.type == AssetType.service:
            meta = asset.asset_metadata or {}
            if meta.get("exposed") and meta.get("sensitive"):
                stats["sensitive_exposed_services"] += 1

        if asset.type == AssetType.technology:
            meta = asset.asset_metadata or {}
            if meta.get("eol") or meta.get("end_of_life"):
                stats["eol_technologies"] += 1

    return stats


async def generate_report_markdown(assets: list[Asset], stats: dict[str, Any]) -> str:
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "Write a professional attack-surface inventory report in Markdown.\n\n"
                f"{GROUNDING_RULES}\n\n"
                "Use the statistics block exactly as given — do not recount or guess numbers.\n"
                "Sections: Executive Summary, Inventory Overview, Certificate & Service Risks, Recommendations.\n"
                "Keep it concise and actionable for a security team.",
            ),
            (
                "human",
                "Statistics (authoritative):\n{stats}\n\nSample assets (up to 100):\n{assets}",
            ),
        ]
    )
    chain = prompt | get_llm()
    response = await chain.ainvoke(
        {"stats": str(stats), "assets": assets_to_context(assets[:100])}
    )
    return response.content if hasattr(response, "content") else str(response)


async def run_report_generation(
    db: AsyncSession, filters: dict[str, Any] | None = None
) -> tuple[str, int, datetime]:
    assets = await fetch_assets_for_report(db, filters)
    stats = compute_inventory_stats(assets)
    markdown = await generate_report_markdown(assets, stats)
    generated_at = datetime.now(timezone.utc)
    return markdown, len(assets), generated_at
