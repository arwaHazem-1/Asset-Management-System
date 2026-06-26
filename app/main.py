import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import init_db
from app.routers import analyze, assets

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Docker runs Alembic first; SQLite/local dev can bootstrap tables on startup.
    if app.state.bootstrap_db or settings.database_url.startswith("sqlite"):
        await init_db()
    yield


app = FastAPI(
    title="DarkAtlas Asset Management",
    description=(
        "A slice of Buguard's DarkAtlas attack surface platform — ingest discovered assets, "
        "track their lifecycle and relationships, and run LangChain-powered analysis over real inventory data."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.state.bootstrap_db = False

app.include_router(assets.router)
app.include_router(analyze.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "Request failed", "detail": str(exc.detail)},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": "Validation error", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": "Something went wrong on our side."},
    )


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}
