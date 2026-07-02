# DarkAtlas Asset Management


DarkAtlas is Buguard's attack surface monitoring platform. This repo is a self-contained slice of its **Asset Management** module: ingest scan results, deduplicate them, track lifecycle and relationships in PostgreSQL, and run four LangChain analysis features on top of the *actual* stored data — never on whatever the model feels like making up.

I'm not building scanners or wiring into a live DarkAtlas deployment. The focus is modeling asset data well and making the AI layer useful and trustworthy.

---

## Getting started

**Recommended (matches the task spec):**

```bash
cd asset-management
cp .env.example .env
# paste your Anthropic key into .env

docker compose up --build
```

Then open:

- API docs → http://localhost:8000/docs  
- Health → http://localhost:8000/health  

Alembic migrations run automatically when the container starts.

**Seed the database:**

```bash
curl -X POST http://localhost:8000/assets/import \
  -H "Content-Type: application/json" \
  -d @sample_data/assets.json
```

There's also `sample_data/appendix_excerpt.json` — the exact shape from the task PDF's Appendix A, useful for checking that `id` / `parent` / `covers` references work.

**Without Docker** (what I used when Docker wasn't installed locally):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL=sqlite+aiosqlite:///./darkatlas.db
export ANTHROPIC_API_KEY=your_key

uvicorn app.main:app --reload --port 8000
```

SQLite auto-creates tables on startup. Production path is still PostgreSQL via Docker.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Async SQLAlchemy URL. Docker default: `postgresql+asyncpg://postgres:password@db:5432/darkatlas` |
| `ANTHROPIC_API_KEY` | For `/analyze/*` | Anthropic API key for Claude (`claude-sonnet-4-6`) |

Copy `.env.example` → `.env`. Don't commit `.env`.

---

## What's implemented (mapped to the task)

### Domain model (Section 3)

- **Asset** — all six types (`domain`, `subdomain`, `ip_address`, `service`, `certificate`, `technology`), lifecycle fields (`first_seen`, `last_seen`, `status`), tags, JSON metadata.
- **AssetRelationship** — graph edges: `subdomain_of`, `covers`, `resolves_to`, `runs_on`, etc.
- Unique constraint on `(type, value)` for deduplication.

### Minimal API + persistence (Section 5)

| Endpoint | What it does |
|----------|--------------|
| `POST /assets/import` | Bulk ingest from scan JSON |
| `GET /assets` | List with filters, sorting, pagination |
| `GET /assets/{id}` | Asset + relationship edges + neighboring nodes |
| `PATCH /assets/{id}` | Update status / tags / metadata |
| `POST /assets/{id}/stale` | Mark stale (flips back to active on re-import) |
| `DELETE /assets/{id}` | Soft delete → `archived` |
| `POST /analyze/query` | Natural language → structured filter → SQL |
| `POST /analyze/risk` | Risk score + flags + summary |
| `POST /analyze/enrich` | Environment / category / criticality |
| `POST /analyze/report` | Markdown inventory report |

Full schemas live at `/docs`.

### Four LangChain features (Section 5 — mandatory)

Every feature follows the same pattern:

1. **Fetch real rows from Postgres** (or accept explicit JSON for enrich-only mode).
2. **Pass them as context** to a LangChain prompt + `ChatAnthropic`.
3. **Return grounded results** — if nothing matches, say so.

Structured outputs use `PydanticOutputParser` where it makes sense (query filters, risk scores, enrichment).

Certificate expiry logic (`expires_before` / `expires_after`, 30-day window) and inventory counts are computed in **Python**, not delegated to the LLM.

### Edge cases (Section 7)

| Case | How it's handled |
|------|------------------|
| Idempotent imports | Same `(type, value)` → update, not duplicate |
| Conflicting metadata | Later import wins on scalars; nested dicts merge |
| Stale asset reappears | Status reset to `active` on import |
| Bad import rows | Skipped individually; batch continues |
| Large inventories | Default `limit=50`, max 500 |
| Cert dates | Expired vs expiring-soon handled in risk + report stats |
| Ambiguous NL queries | `out_of_scope` flag + empty results + message |
| Hallucination | Grounding rules in every prompt; DB query before LLM |

### Deliverables (Section 8)

- Source code, `docker-compose.yml`, Alembic migrations, tests, this README.
- Example prompts below.
- GitHub Actions workflow (`.github/workflows/tests.yml`) runs pytest on push.

---

## Example prompts & sample outputs

These assume you've imported `sample_data/assets.json`.

### 1. Natural language query

```bash
curl -X POST http://localhost:8000/analyze/query \
  -H "Content-Type: application/json" \
  -d '{"question": "show me expired production certificates"}'
```

**Typical response shape:**

```json
{
  "assets": [
    {
      "type": "certificate",
      "value": "CN=api.buguard.io",
      "tags": ["prod"],
      "metadata": { "expires": "2024-11-15T00:00:00Z" }
    }
  ],
  "message": null,
  "filters_applied": {
    "type": "certificate",
    "tags": ["prod"],
    "metadata_conditions": { "expires_before": "2026-06-24" }
  }
}
```

Out-of-scope question → empty `assets`, helpful `message`.

### 2. Risk scoring

```bash
curl -X POST http://localhost:8000/analyze/risk \
  -H "Content-Type: application/json" \
  -d '{"filters": {"type": "certificate"}}'
```

```json
{
  "analysis": {
    "risk_score": "high",
    "flags": [
      "Expired certificate: CN=*.buguard.io",
      "Certificate expiring within 30 days: CN=staging.buguard.io"
    ],
    "summary": "Several TLS certificates are expired or nearing expiry..."
  },
  "asset_count": 4,
  "assets": ["..."]
}
```

Flags are pre-computed in Python; Claude writes the summary from those facts.

### 3. Enrichment

```bash
# By stored asset ID (persists to metadata)
curl -X POST http://localhost:8000/analyze/enrich \
  -H "Content-Type: application/json" \
  -d '{"asset_id": "<uuid from GET /assets>"}'

# Or classify before import
curl -X POST http://localhost:8000/analyze/enrich \
  -H "Content-Type: application/json" \
  -d '{"asset": {"type": "subdomain", "value": "api.buguard.io", "tags": ["prod"]}}'
```

### 4. Report

```bash
curl -X POST http://localhost:8000/analyze/report \
  -H "Content-Type: application/json" \
  -d '{"filters": {"tag": "prod"}}'
```

Returns `{ "report_markdown": "# ...", "asset_count": N, "generated_at": "..." }`.

---

## Design decisions & assumptions

1. **Deduplication key** — `(type, value)` is the canonical identity. Export `id` fields (like `"a1"`) are stored as `metadata.scan_id` for relationship resolution, not as primary keys.

2. **Two-pass import** — upsert all assets first, then wire relationships. That way `parent: "a1"` works even when `"a1"` appears later in the file.

3. **Metadata merge** — if the same asset shows up from `scan` and `manual` with different banners, the newer import overwrites scalar fields but merges nested objects. Documented because real ASM data is messy.

4. **`metadata` column name** — SQLAlchemy reserves `metadata` on the declarative base, so the Python attribute is `asset_metadata`.

5. **Soft delete** — `DELETE` sets `archived`. Archived assets are excluded from analysis queries.

6. **No auth** — Track B doesn't require it (Track A does). In production I'd add API keys on write paths.

7. **No multi-tenancy** — single-org scope. Would add `organization_id` everywhere for the bonus stretch goal.

8. **SQLite in tests** — pytest uses in-memory SQLite with dialect-compatible types so CI doesn't need Postgres. Docker/production uses real PostgreSQL + Alembic.

9. **Relationship types** — import accepts `parent`, `covers`, `resolves_to`, `runs_on`. Other edge types can be added the same way.

---

## Running tests

```bash
pip install -r requirements.txt
pytest -v
```

Tests mock all Claude calls. They cover dedup, idempotent re-import, metadata merge, stale reactivation, malformed rows, pagination, value search, appendix-style IDs, relationships/graph, and all four analyze endpoints.

---

## What I'd do next (if I had more time)

- **Agentic analysis** — LangChain agent that calls `/assets` and `/analyze` as tools instead of inline DB access.
- **Output evaluation** — golden-set harness to regression-test prompt quality.
- **Multi-tenant scoping** — `organization_id` on assets + row-level filtering.
- **Graph visualization** — simple endpoint returning nodes/edges for a frontend.
- **Rate limiting** — protect `/analyze/*` from runaway LLM costs.

---

## Project layout

```
asset-management/
├── app/
│   ├── main.py, database.py, models.py, schemas.py
│   ├── routers/          # assets + analyze
│   └── langchain/        # query, risk, enrichment, report
├── tests/
├── sample_data/
├── alembic/
├── docker-compose.yml
└── Dockerfile
```

---

## Errors

Everything returns a consistent shape:

```json
{ "error": "short label", "detail": "what went wrong" }
```

LLM failures → HTTP 500, `"error": "AI analysis failed"`. No stack traces leaked to clients. Secrets are never logged.
