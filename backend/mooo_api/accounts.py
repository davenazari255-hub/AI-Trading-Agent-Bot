"""Operator Account storage. A deployment has at most one Operator Account."""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mooo_core.models import OperatorAccount


class AccountExistsError(Exception):
    """An Operator Account already exists, so First-Run Setup is closed."""


@dataclass(frozen=True, slots=True)
class OperatorRecord:
    id: uuid.UUID
    username: str
    password_hash: str


class OperatorAccountStore(Protocol):
    async def get(self) -> OperatorRecord | None: ...

    async def create(self, username: str, password_hash: str) -> OperatorRecord: ...


class SqlOperatorAccountStore:
    """Stores the Operator Account in the ``operator_accounts`` table.

    The table has a unique ``singleton`` column, so a second account cannot be
    written even when two setup requests race.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self) -> OperatorRecord | None:
        async with self._session_factory() as session:
            row = await session.scalar(sa.select(OperatorAccount).limit(1))
        if row is None:
            return None
        return OperatorRecord(id=row.id, username=row.username, password_hash=row.password_hash)

    async def create(self, username: str, password_hash: str) -> OperatorRecord:
        record = OperatorRecord(id=uuid.uuid4(), username=username, password_hash=password_hash)
        async with self._session_factory() as session:
            existing = await session.scalar(sa.select(OperatorAccount.id).limit(1))
            if existing is not None:
                raise AccountExistsError
            session.add(
                OperatorAccount(
                    id=record.id,
                    username=record.username,
                    password_hash=record.password_hash,
                    singleton=True,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise AccountExistsError from exc
        return record


class InMemoryOperatorAccountStore:
    """In-memory store for tests and local tools."""

    def __init__(self) -> None:
        self._record: OperatorRecord | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> OperatorRecord | None:
        return self._record

    async def create(self, username: str, password_hash: str) -> OperatorRecord:
        async with self._lock:
            if self._record is not None:
                raise AccountExistsError
            self._record = OperatorRecord(
                id=uuid.uuid4(), username=username, password_hash=password_hash
            )
            return self._record
