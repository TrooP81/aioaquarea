"""Async database engine and session factory."""

from __future__ import annotations

import asyncio
import functools
import copy
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def copy_materialized_rows(rows, column_names, *, context=None):
    """Copy loaded values cooperatively without retaining ORM/session objects."""
    copied = []
    claim = (context or {}).get("_readiness_copy_claim")
    for index, row in enumerate(rows):
        item = SimpleNamespace(
            **{name: copy.deepcopy(getattr(row, name, None)) for name in column_names}
        )
        if claim is not None:
            claim(item)
        copied.append(item)
        if index % 256 == 255:
            await asyncio.sleep(0)
    return copied


@asynccontextmanager
async def get_session(
    *, read_only: bool = False, isolation_level: str | None = None
) -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory(autoflush=not read_only) as session:
        try:
            if read_only or isolation_level is not None:
                options = {}
                if isolation_level is not None:
                    options["isolation_level"] = isolation_level
                if read_only:
                    options["postgresql_readonly"] = True
                await session.connection(execution_options=options)
            if read_only:
                await session.execute(text("SET LOCAL statement_timeout = '2s'"))
            yield session
            if read_only:
                await session.rollback()
            else:
                await session.commit()
        except BaseException:
            await session.rollback()
            raise


def retry_on_transient(max_retries: int = 3, backoff: float = 0.5):
    """Decorator that retries an async function on transient DB errors (OperationalError)."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except OperationalError:
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(backoff * (attempt + 1))

        return wrapper

    return decorator
