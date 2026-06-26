import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Asset, AssetRelationship, AssetStatus, AssetType
from app.schemas import (
    AssetDetailOut,
    AssetImportItem,
    AssetOut,
    AssetRelationshipOut,
    AssetUpdate,
    ImportResult,
    PaginatedAssets,
)
from app.utils import apply_tag_filter, apply_value_contains

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/assets", tags=["assets"])


def _merge_tags(existing: list[str] | None, incoming: list[str] | None) -> list[str]:
    merged = list(existing or [])
    for tag in incoming or []:
        if tag not in merged:
            merged.append(tag)
    return merged


def _merge_metadata(existing: dict | None, incoming: dict | None) -> dict:
    """Later imports win on scalar fields; nested dicts are merged shallowly."""
    base = dict(existing or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


async def _find_asset_by_type_value(
    db: AsyncSession, asset_type: AssetType, value: str
) -> Asset | None:
    result = await db.execute(
        select(Asset).where(Asset.type == asset_type, Asset.value == value)
    )
    return result.scalar_one_or_none()


async def _find_asset_by_scan_id(db: AsyncSession, scan_id: str) -> Asset | None:
    result = await db.execute(select(Asset))
    for asset in result.scalars():
        if (asset.asset_metadata or {}).get("scan_id") == scan_id:
            return asset
    return None


async def _ensure_relationship(
    db: AsyncSession,
    source: Asset,
    target: Asset,
    relationship_type: str,
) -> None:
    if source.id == target.id:
        return
    result = await db.execute(
        select(AssetRelationship).where(
            AssetRelationship.source_id == source.id,
            AssetRelationship.target_id == target.id,
            AssetRelationship.relationship_type == relationship_type,
        )
    )
    if result.scalar_one_or_none() is None:
        db.add(
            AssetRelationship(
                source_id=source.id,
                target_id=target.id,
                relationship_type=relationship_type,
            )
        )


async def _resolve_ref(
    db: AsyncSession,
    ref: str,
    batch_assets: dict[str, Asset],
) -> Asset | None:
    """Resolve a relationship pointer: UUID, export id, canonical value, or same-batch key."""
    if ref in batch_assets:
        return batch_assets[ref]

    try:
        asset_id = UUID(ref)
        result = await db.execute(select(Asset).where(Asset.id == asset_id))
        asset = result.scalar_one_or_none()
        if asset:
            return asset
    except ValueError:
        pass

    by_scan = await _find_asset_by_scan_id(db, ref)
    if by_scan:
        return by_scan

    result = await db.execute(select(Asset).where(Asset.value == ref))
    return result.scalar_one_or_none()


async def _link_if_found(
    db: AsyncSession,
    source: Asset,
    ref: str | None,
    relationship_type: str,
    batch_assets: dict[str, Asset],
    index: int,
    errors: list[str],
    label: str,
) -> None:
    if not ref:
        return
    target = await _resolve_ref(db, ref, batch_assets)
    if target:
        await _ensure_relationship(db, source, target, relationship_type)
    else:
        errors.append(f"Row {index}: {label} '{ref}' could not be matched to an asset")


@router.post(
    "/import",
    response_model=ImportResult,
    summary="Bulk import assets from a scan export",
    description=(
        "Ingests a JSON array of assets from DarkAtlas (or the provided sample dataset). "
        "Duplicate `(type, value)` pairs update `last_seen` and merge tags/metadata instead of "
        "creating a second row. Bad rows are skipped without failing the whole batch."
    ),
)
async def import_assets(
    payload: list[dict[str, Any]],
    db: AsyncSession = Depends(get_db),
) -> ImportResult:
    imported = updated = skipped = 0
    errors: list[str] = []
    parsed: list[tuple[int, AssetImportItem]] = []
    batch_assets: dict[str, Asset] = {}

    # Pass 1 — upsert everything so relationship targets exist.
    for index, raw in enumerate(payload):
        try:
            item = AssetImportItem.model_validate(raw)
        except Exception as exc:
            skipped += 1
            msg = f"Row {index}: skipped — {exc}"
            logger.warning(msg)
            errors.append(msg)
            continue

        parsed.append((index, item))
        existing = await _find_asset_by_type_value(db, item.type, item.value)
        now = datetime.now(timezone.utc)
        metadata = dict(item.metadata or {})
        if item.external_id:
            metadata["scan_id"] = item.external_id

        if existing:
            existing.last_seen = now
            existing.tags = _merge_tags(existing.tags, item.tags)
            existing.asset_metadata = _merge_metadata(existing.asset_metadata, metadata)
            if existing.status == AssetStatus.stale:
                existing.status = AssetStatus.active
            asset = existing
            updated += 1
        else:
            asset = Asset(
                type=item.type,
                value=item.value,
                status=item.status,
                source=item.source,
                tags=item.tags or [],
                asset_metadata=metadata,
                first_seen=now,
                last_seen=now,
            )
            db.add(asset)
            await db.flush()
            imported += 1

        batch_assets[item.value] = asset
        if item.external_id:
            batch_assets[item.external_id] = asset

    # Pass 2 — wire up the relationship graph.
    for index, item in parsed:
        asset = batch_assets[item.value]

        if item.parent:
            parent = await _resolve_ref(db, item.parent, batch_assets)
            if parent:
                rel_type = "subdomain_of" if item.type == AssetType.subdomain else "child_of"
                await _ensure_relationship(db, asset, parent, rel_type)
            else:
                errors.append(f"Row {index}: parent '{item.parent}' could not be matched")

        await _link_if_found(db, asset, item.covers, "covers", batch_assets, index, errors, "covers")
        await _link_if_found(db, asset, item.resolves_to, "resolves_to", batch_assets, index, errors, "resolves_to")
        await _link_if_found(db, asset, item.runs_on, "runs_on", batch_assets, index, errors, "runs_on")

    return ImportResult(imported=imported, updated=updated, skipped=skipped, errors=errors)


@router.get(
    "",
    response_model=PaginatedAssets,
    summary="List assets with filters and pagination",
)
async def list_assets(
    type: AssetType | None = None,
    status: AssetStatus | None = None,
    tag: str | None = None,
    value_contains: str | None = Query(None, description="Case-insensitive substring match on value"),
    sort_by: str = Query("last_seen", pattern="^(last_seen|first_seen|value|type)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> PaginatedAssets:
    query = select(Asset)
    count_query = select(func.count()).select_from(Asset)

    if type:
        query = query.where(Asset.type == type)
        count_query = count_query.where(Asset.type == type)
    if status:
        query = query.where(Asset.status == status)
        count_query = count_query.where(Asset.status == status)
    if tag:
        query = await apply_tag_filter(query, tag, db)
        count_query = await apply_tag_filter(count_query, tag, db)
    if value_contains:
        query = apply_value_contains(query, value_contains)
        count_query = apply_value_contains(count_query, value_contains)

    sort_column = getattr(Asset, sort_by)
    query = query.order_by(sort_column.desc() if sort_order == "desc" else sort_column.asc())
    query = query.limit(limit).offset(offset)

    total = (await db.execute(count_query)).scalar_one()
    result = await db.execute(query)
    assets = result.scalars().all()

    return PaginatedAssets(
        items=[AssetOut.from_orm_asset(a) for a in assets],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _load_asset_graph(db: AsyncSession, asset_id: UUID) -> tuple[Asset, list[AssetRelationship], list[Asset]]:
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        return None, [], []

    rel_result = await db.execute(
        select(AssetRelationship).where(
            or_(
                AssetRelationship.source_id == asset_id,
                AssetRelationship.target_id == asset_id,
            )
        )
    )
    relationships = list(rel_result.scalars().all())

    neighbor_ids = {
        rel.target_id if rel.source_id == asset_id else rel.source_id for rel in relationships
    }
    related: list[Asset] = []
    if neighbor_ids:
        neighbors = await db.execute(select(Asset).where(Asset.id.in_(neighbor_ids)))
        related = list(neighbors.scalars().all())

    return asset, relationships, related


@router.get(
    "/{asset_id}",
    response_model=AssetDetailOut,
    summary="Get one asset and its local relationship graph",
)
async def get_asset(asset_id: UUID, db: AsyncSession = Depends(get_db)) -> AssetDetailOut:
    asset, relationships, related = await _load_asset_graph(db, asset_id)
    if not asset:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "detail": f"No asset with id {asset_id}"},
        )

    base = AssetOut.from_orm_asset(asset)
    return AssetDetailOut(
        **base.model_dump(),
        relationships=[AssetRelationshipOut.model_validate(r) for r in relationships],
        related_assets=[AssetOut.from_orm_asset(r) for r in related],
    )


@router.patch(
    "/{asset_id}",
    response_model=AssetOut,
    summary="Update status, tags, or metadata",
)
async def update_asset(
    asset_id: UUID,
    payload: AssetUpdate,
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "detail": f"No asset with id {asset_id}"},
        )

    if payload.status is not None:
        asset.status = payload.status
    if payload.tags is not None:
        asset.tags = payload.tags
    if payload.metadata is not None:
        asset.asset_metadata = _merge_metadata(asset.asset_metadata, payload.metadata)

    asset.last_seen = datetime.now(timezone.utc)
    return AssetOut.from_orm_asset(asset)


@router.post(
    "/{asset_id}/stale",
    response_model=AssetOut,
    summary="Mark an asset as stale",
    description="Explicit lifecycle transition — stale assets flip back to active if seen again on import.",
)
async def mark_asset_stale(asset_id: UUID, db: AsyncSession = Depends(get_db)) -> AssetOut:
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "detail": f"No asset with id {asset_id}"},
        )

    asset.status = AssetStatus.stale
    asset.last_seen = datetime.now(timezone.utc)
    return AssetOut.from_orm_asset(asset)


@router.delete(
    "/{asset_id}",
    response_model=AssetOut,
    summary="Archive an asset (soft delete)",
)
async def delete_asset(asset_id: UUID, db: AsyncSession = Depends(get_db)) -> AssetOut:
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(
            status_code=404,
            detail={"error": "Not found", "detail": f"No asset with id {asset_id}"},
        )

    asset.status = AssetStatus.archived
    asset.last_seen = datetime.now(timezone.utc)
    return AssetOut.from_orm_asset(asset)
