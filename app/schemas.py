from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models import AssetStatus, AssetType


class AssetBase(BaseModel):
    type: AssetType
    value: str
    status: AssetStatus = AssetStatus.active
    source: str = "import"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetImportItem(AssetBase):
    """One row from a DarkAtlas scan export.

    Relationship hints (`parent`, `covers`, etc.) accept another asset's value,
    UUID, or the short `id` from the export file (stored as metadata.scan_id).
    """

    external_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("id", "external_id"),
        description="Optional ID from the source export, e.g. 'a1' in the task appendix.",
    )
    parent: str | None = Field(default=None, description="Domain or parent asset reference.")
    covers: str | None = Field(default=None, description="Asset this certificate covers.")
    resolves_to: str | None = Field(default=None, description="IP or host this name resolves to.")
    runs_on: str | None = Field(default=None, description="Host/IP where a service or tech runs.")
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AssetUpdate(BaseModel):
    status: AssetStatus | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class AssetRelationshipOut(BaseModel):
    id: UUID
    source_id: UUID
    target_id: UUID
    relationship_type: str
    model_config = ConfigDict(from_attributes=True)


class AssetOut(BaseModel):
    id: UUID
    type: AssetType
    value: str
    status: AssetStatus
    first_seen: datetime
    last_seen: datetime
    source: str
    tags: list[str]
    metadata: dict[str, Any]
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_asset(cls, asset) -> "AssetOut":
        return cls(
            id=asset.id,
            type=asset.type,
            value=asset.value,
            status=asset.status,
            first_seen=asset.first_seen,
            last_seen=asset.last_seen,
            source=asset.source,
            tags=asset.tags or [],
            metadata=asset.asset_metadata or {},
        )


class AssetDetailOut(AssetOut):
    """Single asset plus its relationship edges and the neighboring nodes in the graph."""

    relationships: list[AssetRelationshipOut] = Field(default_factory=list)
    related_assets: list[AssetOut] = Field(default_factory=list)


class ImportResult(BaseModel):
    imported: int
    updated: int
    skipped: int
    errors: list[str]


class PaginatedAssets(BaseModel):
    items: list[AssetOut]
    total: int
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    error: str
    detail: str | list[Any]


# --- Analyze endpoints ---


class QueryRequest(BaseModel):
    question: str = Field(
        ...,
        examples=["show me all expired certificates tagged prod"],
    )


class QueryFilter(BaseModel):
    type: AssetType | None = None
    status: AssetStatus | None = None
    tags: list[str] = Field(default_factory=list)
    value_contains: str | None = None
    metadata_conditions: dict[str, Any] = Field(default_factory=dict)
    out_of_scope: bool = False
    message: str | None = None


class QueryResponse(BaseModel):
    assets: list[AssetOut] = Field(default_factory=list)
    message: str | None = None
    filters_applied: QueryFilter | None = None


class RiskRequest(BaseModel):
    asset_ids: list[UUID] | None = None
    filters: dict[str, Any] | None = None


class RiskAnalysis(BaseModel):
    risk_score: str = Field(description="One of: low, medium, high, critical")
    flags: list[str]
    summary: str


class RiskResponse(BaseModel):
    analysis: RiskAnalysis
    asset_count: int
    assets: list[AssetOut]


class EnrichRequest(BaseModel):
    asset_id: UUID | None = None
    asset: AssetImportItem | None = None


class EnrichmentResult(BaseModel):
    environment: str = Field(description="production | staging | development | unknown")
    category: str = Field(description="web | infrastructure | security | data | other")
    criticality: str = Field(description="critical | high | medium | low")


class EnrichResponse(BaseModel):
    asset: AssetOut | AssetImportItem
    enrichment: EnrichmentResult
    persisted: bool = False


class ReportRequest(BaseModel):
    filters: dict[str, Any] | None = Field(
        default=None,
        description="Optional filters: type, status, tag, value_contains",
    )


class ReportResponse(BaseModel):
    report_markdown: str
    asset_count: int
    generated_at: datetime
