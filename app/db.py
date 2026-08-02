"""
Database layer.

Async SQLAlchemy 2.0 over SQLite for local dev. When you deploy, change
DATABASE_URL to a Postgres URL (e.g. postgresql+asyncpg://...) and nothing
else here needs to change — that's the point of keeping this isolated.
"""

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    """All ORM models inherit from this."""
    pass


engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Create tables at startup, then add any columns the models gained since
    the database file was made.

    `create_all` only creates MISSING TABLES — it never alters existing ones. So
    adding a column to a model leaves older databases broken with
    "table X has no column named Y" until someone deletes the file. This closes
    that gap for the common, safe case: new nullable / defaulted columns.

    Deliberately limited. It will not drop columns, change types, or rename
    anything, because those need real thought about existing rows. If you ever
    need one of those, that's the point to bring in Alembic.
    """
    from . import models  # noqa: F401  (import so models register on Base)
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        is_sqlite = settings.database_url.startswith("sqlite")
        for table in Base.metadata.sorted_tables:
            if is_sqlite:
                rows = await conn.execute(text(f"PRAGMA table_info('{table.name}')"))
                existing = {r[1] for r in rows}
            else:
                rows = await conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
                ), {"t": table.name})
                existing = {r[0] for r in rows}
            if not existing:
                continue                      # table was just created; nothing to patch

            for col in table.columns:
                if col.name in existing:
                    continue
                # only safe to bolt on if existing rows can be given a value
                if not col.nullable and col.default is None and col.server_default is None:
                    print(f"[db] SKIP {table.name}.{col.name}: NOT NULL with no default. "
                          f"Add it by hand or recreate the table.")
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if default is not None:
                    ddl += " DEFAULT " + (f"'{default}'" if isinstance(default, str) else str(int(default)))
                try:
                    await conn.execute(text(ddl))
                    print(f"[db] migrated: added {table.name}.{col.name}")
                except Exception as e:
                    print(f"[db] could not add {table.name}.{col.name}: {type(e).__name__}: {e}")


async def get_session() -> AsyncSession:
    """FastAPI dependency: one session per request, always closed."""
    async with SessionLocal() as session:
        yield session
