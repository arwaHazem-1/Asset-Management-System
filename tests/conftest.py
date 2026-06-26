import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.main import app


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def client(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def sample_assets():
    return [
        {
            "type": "domain",
            "value": "example.com",
            "source": "import",
            "tags": ["prod"],
            "metadata": {"environment": "production"},
        },
        {
            "type": "subdomain",
            "value": "api.example.com",
            "source": "import",
            "tags": ["prod"],
            "parent": "example.com",
        },
        {
            "type": "certificate",
            "value": "CN=example.com",
            "source": "import",
            "tags": ["prod"],
            "covers": "example.com",
            "metadata": {"expires": "2025-01-01T00:00:00Z"},
        },
    ]


@pytest.fixture
def appendix_assets():
    """Shape from the task document's Appendix A."""
    return [
        {
            "id": "a1",
            "type": "domain",
            "value": "example.com",
            "status": "active",
            "source": "scan",
            "tags": ["root"],
            "metadata": {},
        },
        {
            "id": "a2",
            "type": "subdomain",
            "value": "api.example.com",
            "status": "active",
            "source": "scan",
            "tags": ["prod"],
            "metadata": {},
            "parent": "a1",
        },
        {
            "id": "a3",
            "type": "certificate",
            "value": "CN=api.example.com",
            "status": "active",
            "source": "scan",
            "tags": [],
            "metadata": {"issuer": "Let's Encrypt", "expires": "2025-01-02"},
            "covers": "a2",
        },
    ]
