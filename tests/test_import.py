import pytest


@pytest.mark.asyncio
async def test_bulk_import(client, sample_assets):
    response = await client.post("/assets/import", json=sample_assets)
    assert response.status_code == 200
    data = response.json()
    assert data["imported"] == 3
    assert data["updated"] == 0
    assert data["skipped"] == 0


@pytest.mark.asyncio
async def test_import_deduplication(client, sample_assets):
    await client.post("/assets/import", json=sample_assets)
    updated_payload = [
        {
            **sample_assets[0],
            "tags": ["prod", "critical"],
            "metadata": {"owner": "security-team"},
        }
    ]
    response = await client.post("/assets/import", json=updated_payload)
    data = response.json()
    assert data["imported"] == 0
    assert data["updated"] == 1

    list_response = await client.get("/assets", params={"type": "domain"})
    items = list_response.json()["items"]
    assert len(items) == 1
    assert "critical" in items[0]["tags"]
    assert items[0]["metadata"]["owner"] == "security-team"


@pytest.mark.asyncio
async def test_idempotent_double_import(client, sample_assets):
    first = await client.post("/assets/import", json=sample_assets)
    second = await client.post("/assets/import", json=sample_assets)
    assert first.json()["imported"] == 3
    assert second.json()["imported"] == 0
    assert second.json()["updated"] == 3

    total = (await client.get("/assets")).json()["total"]
    assert total == 3


@pytest.mark.asyncio
async def test_conflicting_metadata_merge(client):
    payload = [
        {
            "type": "service",
            "value": "https://app.example.com",
            "source": "scan",
            "metadata": {"banner": "nginx", "port": 443},
        }
    ]
    await client.post("/assets/import", json=payload)
    await client.post(
        "/assets/import",
        json=[
            {
                "type": "service",
                "value": "https://app.example.com",
                "source": "manual",
                "metadata": {"banner": "nginx/1.24", "tls": "1.3"},
            }
        ],
    )
    item = (await client.get("/assets")).json()["items"][0]
    assert item["metadata"]["banner"] == "nginx/1.24"
    assert item["metadata"]["port"] == 443
    assert item["metadata"]["tls"] == "1.3"


@pytest.mark.asyncio
async def test_import_malformed_record(client, sample_assets):
    payload = sample_assets + [{"type": "invalid_type", "value": "bad.example.com"}]
    response = await client.post("/assets/import", json=payload)
    data = response.json()
    assert data["imported"] == 3
    assert data["skipped"] == 1
    assert len(data["errors"]) == 1


@pytest.mark.asyncio
async def test_stale_asset_reactivation(client):
    create = await client.post(
        "/assets/import",
        json=[
            {
                "type": "service",
                "value": "https://legacy.example.com",
                "source": "scan",
                "status": "stale",
                "tags": ["prod"],
            }
        ],
    )
    assert create.json()["imported"] == 1

    asset_id = (await client.get("/assets")).json()["items"][0]["id"]
    await client.post(f"/assets/{asset_id}/stale")

    reimport = await client.post(
        "/assets/import",
        json=[
            {
                "type": "service",
                "value": "https://legacy.example.com",
                "source": "scan",
                "tags": ["prod"],
            }
        ],
    )
    assert reimport.json()["updated"] == 1

    detail = await client.get(f"/assets/{asset_id}")
    assert detail.json()["status"] == "active"


@pytest.mark.asyncio
async def test_import_relationships_and_graph(client, sample_assets):
    await client.post("/assets/import", json=sample_assets)
    subdomain = await client.get("/assets", params={"type": "subdomain"})
    subdomain_id = subdomain.json()["items"][0]["id"]
    detail = await client.get(f"/assets/{subdomain_id}")
    body = detail.json()
    assert len(body["relationships"]) >= 1
    assert len(body["related_assets"]) >= 1


@pytest.mark.asyncio
async def test_appendix_style_external_ids(client, appendix_assets):
    response = await client.post("/assets/import", json=appendix_assets)
    assert response.status_code == 200
    assert response.json()["imported"] == 3
    assert response.json()["errors"] == []

    cert = await client.get("/assets", params={"type": "certificate"})
    cert_id = cert.json()["items"][0]["id"]
    detail = await client.get(f"/assets/{cert_id}")
    rel_types = {r["relationship_type"] for r in detail.json()["relationships"]}
    assert "covers" in rel_types


@pytest.mark.asyncio
async def test_list_pagination(client, sample_assets):
    await client.post("/assets/import", json=sample_assets)
    page = await client.get("/assets", params={"limit": 2, "offset": 0})
    data = page.json()
    assert data["total"] == 3
    assert len(data["items"]) == 2

    page2 = await client.get("/assets", params={"limit": 2, "offset": 2})
    assert len(page2.json()["items"]) == 1


@pytest.mark.asyncio
async def test_value_contains_filter(client, sample_assets):
    await client.post("/assets/import", json=sample_assets)
    response = await client.get("/assets", params={"value_contains": "api.example"})
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["value"] == "api.example.com"
