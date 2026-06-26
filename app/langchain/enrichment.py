import json
from uuid import UUID

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.langchain.query import GROUNDING_RULES, get_llm
from app.models import Asset
from app.schemas import AssetImportItem, AssetOut, EnrichmentResult


async def enrich_asset_data(asset_payload: dict) -> EnrichmentResult:
    parser = PydanticOutputParser(pydantic_object=EnrichmentResult)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "Classify a single discovered asset for a security inventory.\n\n"
                f"{GROUNDING_RULES}\n\n"
                "Infer from tags, hostname patterns, type, and metadata only:\n"
                "- environment: production | staging | development | unknown\n"
                "- category: web | infrastructure | security | data | other\n"
                "- criticality: critical | high | medium | low\n\n"
                "{format_instructions}",
            ),
            ("human", "Asset:\n{asset}"),
        ]
    )
    chain = prompt | get_llm() | parser
    return await chain.ainvoke(
        {
            "asset": json.dumps(asset_payload, indent=2, default=str),
            "format_instructions": parser.get_format_instructions(),
        }
    )


async def run_enrichment(
    db: AsyncSession,
    asset_id: UUID | None = None,
    raw_asset: AssetImportItem | None = None,
) -> tuple[AssetOut | AssetImportItem, EnrichmentResult, bool]:
    persisted = False

    if asset_id:
        result = await db.execute(select(Asset).where(Asset.id == asset_id))
        asset = result.scalar_one_or_none()
        if not asset:
            raise ValueError(f"Asset {asset_id} not found")
        payload = {
            "id": str(asset.id),
            "type": asset.type.value,
            "value": asset.value,
            "status": asset.status.value,
            "tags": asset.tags or [],
            "metadata": asset.asset_metadata or {},
        }
        enrichment = await enrich_asset_data(payload)
        meta = dict(asset.asset_metadata or {})
        meta.update(enrichment.model_dump())
        asset.asset_metadata = meta
        persisted = True
        return AssetOut.from_orm_asset(asset), enrichment, persisted

    if raw_asset:
        payload = raw_asset.model_dump()
        enrichment = await enrich_asset_data(payload)
        enriched = raw_asset.model_copy(deep=True)
        enriched.metadata = {**enriched.metadata, **enrichment.model_dump()}
        return enriched, enrichment, persisted

    raise ValueError("Pass either asset_id or a raw asset object")
