from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
        eng = create_engine(url, **kwargs)

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # enforce FKs in SQLite
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng
    if url.startswith("postgres://"):  # some hosts hand out the legacy scheme
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    # 30+ simultaneous punches: each request holds one connection only for a few milliseconds, but the default pool
    # (5 + 10) would make bursts queue. Keep this below the database plan's connection limit.
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_timeout=30)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables for local development. Production uses Alembic migrations."""
    from app.database import models  # noqa: F401

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        models.install_append_only_triggers(conn)
