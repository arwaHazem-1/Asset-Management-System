import json
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.langchain.query import GROUNDING_RULES, assets_to_context, get_llm
from app.models import Asset, AssetStatus, AssetType
from app.schemas import AssetOut, RiskAnalysis
from app.utils import apply_tag_filter, apply_value_contains


def _parse_expiry(meta: dict) -> datetime | None:
    raw = meta.get("expires") or meta.get("expires_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_risk_flags(assets: list[Asset]) -> list[str]:
    """Hard facts we compute in Python — the LLM summarizes these, it doesn't discover them."""
    flags: list[str] = []
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=30)

    for asset in assets:
        if asset.type == AssetType.certificate:
            expires = _parse_expiry(asset.asset_metadata or {})
            if expires and expires < now:
                flags.append(f"Expired certificate: {asset.value}")
            elif expires and expires <= soon:
                flags.append(f"Certificate expiring within 30 days: {asset.value}")

        if asset.type == AssetType.service:
            meta = asset.asset_metadata or {}
            if meta.get("exposed") and meta.get("sensitive"):
                flags.append(f"Sensitive service exposed to the internet: {asset.value}")

        if asset.type == AssetType.technology:
            meta = asset.asset_metadata or {}
            if meta.get("eol") is True or meta.get("end_of_life") is True:
                flags.append(f"End-of-life technology still in use: {asset.value}")

    return flags


async def fetch_assets_for_risk(
    db: AsyncSession,
    asset_ids: list | None = None,
    filters: dict[str, Any] | None = None,
) -> list[Asset]:
    query = select(Asset).where(Asset.status != AssetStatus.archived)

    if asset_ids:
        query = query.where(Asset.id.in_(asset_ids))
    elif filters:
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


async def analyze_risk(assets: list[Asset]) -> RiskAnalysis:
    if not assets:
        return RiskAnalysis(
            risk_score="low",
            flags=[],
            summary="There were no assets in scope for this assessment.",
        )

    deterministic_flags = compute_risk_flags(assets)
    parser = PydanticOutputParser(pydantic_object=RiskAnalysis)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are summarizing security risk for a subset of external-facing assets.\n\n"
                f"{GROUNDING_RULES}\n\n"
                "Use the pre-computed flags and asset JSON as your only evidence.\n"
                "Pick risk_score from: low, medium, high, critical.\n"
                "Your flags list should reflect the provided flags (you may rephrase, not add new ones).\n"
                "Write one short paragraph a SOC analyst would actually read.\n\n"
                "{format_instructions}",
            ),
            (
                "human",
                "Pre-computed flags:\n{flags}\n\nAsset records:\n{assets}",
            ),
        ]
    )
    chain = prompt | get_llm() | parser
    return await chain.ainvoke(
        {
            "flags": json.dumps(deterministic_flags),
            "assets": assets_to_context(assets),
            "format_instructions": parser.get_format_instructions(),
        }
    )


async def run_risk_analysis(
    db: AsyncSession,
    asset_ids: list | None = None,
    filters: dict[str, Any] | None = None,
) -> tuple[RiskAnalysis, list[AssetOut]]:
    assets = await fetch_assets_for_risk(db, asset_ids, filters)
    analysis = await analyze_risk(assets)
    return analysis, [AssetOut.from_orm_asset(a) for a in assets]
