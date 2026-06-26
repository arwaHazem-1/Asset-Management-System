import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.langchain.enrichment import run_enrichment
from app.langchain.query import run_natural_language_query
from app.langchain.report import run_report_generation
from app.langchain.risk import run_risk_analysis
from app.schemas import (
    EnrichRequest,
    EnrichResponse,
    QueryRequest,
    QueryResponse,
    ReportRequest,
    ReportResponse,
    RiskRequest,
    RiskResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analyze", tags=["analyze"])


@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask questions about your inventory in plain English",
)
async def analyze_query(payload: QueryRequest, db: AsyncSession = Depends(get_db)) -> QueryResponse:
    """Claude turns the question into filters; we run a real SQL query and return only matching rows."""
    try:
        assets, message, filters = await run_natural_language_query(db, payload.question)
        return QueryResponse(assets=assets, message=message, filters_applied=filters)
    except Exception as exc:
        logger.exception("Natural language query failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "AI analysis failed", "detail": str(exc)},
        ) from exc


@router.post(
    "/risk",
    response_model=RiskResponse,
    summary="Score risk for specific assets or a filtered group",
)
async def analyze_risk(payload: RiskRequest, db: AsyncSession = Depends(get_db)) -> RiskResponse:
    """Expired certs, exposed services, and EOL tech are flagged in Python first; Claude writes the summary."""
    if not payload.asset_ids and not payload.filters:
        raise HTTPException(
            status_code=422,
            detail={"error": "Validation error", "detail": "Provide asset_ids or filters"},
        )
    try:
        analysis, assets = await run_risk_analysis(db, payload.asset_ids, payload.filters)
        return RiskResponse(analysis=analysis, asset_count=len(assets), assets=assets)
    except Exception as exc:
        logger.exception("Risk analysis failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "AI analysis failed", "detail": str(exc)},
        ) from exc


@router.post(
    "/enrich",
    response_model=EnrichResponse,
    summary="Classify environment, category, and criticality",
)
async def analyze_enrich(payload: EnrichRequest, db: AsyncSession = Depends(get_db)) -> EnrichResponse:
    """Works on a stored asset (and saves enrichment to metadata) or a raw JSON blob before import."""
    if not payload.asset_id and not payload.asset:
        raise HTTPException(
            status_code=422,
            detail={"error": "Validation error", "detail": "Provide asset_id or asset"},
        )
    try:
        asset, enrichment, persisted = await run_enrichment(db, payload.asset_id, payload.asset)
        return EnrichResponse(asset=asset, enrichment=enrichment, persisted=persisted)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"error": "Not found", "detail": str(exc)}) from exc
    except Exception as exc:
        logger.exception("Enrichment failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "AI analysis failed", "detail": str(exc)},
        ) from exc


@router.post(
    "/report",
    response_model=ReportResponse,
    summary="Generate a Markdown inventory / risk report",
)
async def analyze_report(payload: ReportRequest, db: AsyncSession = Depends(get_db)) -> ReportResponse:
    """Stats are counted in Python; Claude turns them into a readable report."""
    try:
        markdown, count, generated_at = await run_report_generation(db, payload.filters)
        return ReportResponse(
            report_markdown=markdown,
            asset_count=count,
            generated_at=generated_at,
        )
    except Exception as exc:
        logger.exception("Report generation failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "AI analysis failed", "detail": str(exc)},
        ) from exc
