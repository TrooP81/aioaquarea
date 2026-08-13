"""Async database engine and session factory."""

from __future__ import annotations

import asyncio
import functools
from contextlib import asynccontextmanager
from typing import AsyncGenerator

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


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
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
