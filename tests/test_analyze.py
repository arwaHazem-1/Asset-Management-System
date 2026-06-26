from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.schemas import EnrichmentResult, QueryFilter, RiskAnalysis


@pytest.mark.asyncio
async def test_analyze_query(client, sample_assets):
    mock_filter = QueryFilter(type="certificate", tags=["prod"])
    with patch(
        "app.langchain.query.parse_natural_language_query",
        new=AsyncMock(return_value=mock_filter),
    ):
        await client.post("/assets/import", json=sample_assets)
        response = await client.post(
            "/analyze/query",
            json={"question": "show production certificates"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["filters_applied"]["type"] == "certificate"
    assert len(data["assets"]) >= 1


@pytest.mark.asyncio
async def test_analyze_query_out_of_scope(client):
    mock_filter = QueryFilter(out_of_scope=True, message="Cannot answer weather questions.")
    with patch(
        "app.langchain.query.parse_natural_language_query",
        new=AsyncMock(return_value=mock_filter),
    ):
        response = await client.post(
            "/analyze/query",
            json={"question": "what is the weather today?"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["assets"] == []
    assert "weather" in data["message"].lower()


@pytest.mark.asyncio
async def test_analyze_risk(client, sample_assets):
    mock_analysis = RiskAnalysis(
        risk_score="high",
        flags=["Expired certificate: CN=example.com"],
        summary="Multiple certificate issues detected in the provided assets.",
    )
    with patch("app.langchain.risk.analyze_risk", new=AsyncMock(return_value=mock_analysis)):
        await client.post("/assets/import", json=sample_assets)
        response = await client.post(
            "/analyze/risk",
            json={"filters": {"type": "certificate"}},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["analysis"]["risk_score"] == "high"
    assert data["asset_count"] >= 1


@pytest.mark.asyncio
async def test_analyze_enrich_persisted(client, sample_assets):
    mock_enrichment = EnrichmentResult(
        environment="production",
        category="web",
        criticality="high",
    )
    with patch(
        "app.langchain.enrichment.enrich_asset_data",
        new=AsyncMock(return_value=mock_enrichment),
    ):
        await client.post("/assets/import", json=sample_assets)
        asset_id = (await client.get("/assets", params={"type": "domain"})).json()["items"][0]["id"]
        response = await client.post("/analyze/enrich", json={"asset_id": asset_id})

    assert response.status_code == 200
    data = response.json()
    assert data["persisted"] is True
    assert data["enrichment"]["environment"] == "production"
    assert data["asset"]["metadata"]["criticality"] == "high"


@pytest.mark.asyncio
async def test_analyze_enrich_raw_asset(client):
    mock_enrichment = EnrichmentResult(
        environment="staging",
        category="infrastructure",
        criticality="medium",
    )
    with patch(
        "app.langchain.enrichment.enrich_asset_data",
        new=AsyncMock(return_value=mock_enrichment),
    ):
        response = await client.post(
            "/analyze/enrich",
            json={
                "asset": {
                    "type": "service",
                    "value": "https://staging.example.com",
                    "tags": ["staging"],
                    "metadata": {},
                }
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["persisted"] is False
    assert data["asset"]["metadata"]["environment"] == "staging"


@pytest.mark.asyncio
async def test_analyze_report(client, sample_assets):
    mock_markdown = "# Security Inventory Report\n\n## Executive Summary\nSample report."
    with patch(
        "app.langchain.report.generate_report_markdown",
        new=AsyncMock(return_value=mock_markdown),
    ):
        await client.post("/assets/import", json=sample_assets)
        response = await client.post("/analyze/report", json={"filters": {"type": "certificate"}})

    assert response.status_code == 200
    data = response.json()
    assert "Security Inventory Report" in data["report_markdown"]
    assert data["asset_count"] >= 1
    assert data["generated_at"]


@pytest.mark.asyncio
async def test_analyze_enrich_not_found(client):
    response = await client.post("/analyze/enrich", json={"asset_id": str(uuid4())})
    assert response.status_code == 404
    assert response.json()["error"] == "Not found"
