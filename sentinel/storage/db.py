"""Async engine, session factory and the health-check ping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinel.core.logging import get_logger

log = get_logger(__name__)


class SupportsPing(Protocol):
    """What ``/health`` needs from the database layer (lets tests inject a stub)."""

    async def ping(self) -> bool: ...
    async def dispose(self) -> None: ...


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def ping(self) -> bool:
        """``SELECT 1``. Never raises — degrade explicitly instead.

        Deliberately broad: an unreachable host surfaces as ``socket.gaierror`` or
        ``OSError`` from the driver, *not* as a :class:`SQLAlchemyError`, and a
        health check that raises turns a 503 ("degraded, here's why") into a 500.
        """
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("db.ping_failed", error=str(exc), error_type=type(exc).__name__)
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()
