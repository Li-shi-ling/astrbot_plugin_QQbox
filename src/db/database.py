from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .tables import (
    CREATE_LAYOUT_PRESET_ACTIVE_INDEX_SQL,
    CREATE_LAYOUT_PRESET_TABLE_SQL,
    CREATE_QQ_PROFILE_TABLE_SQL,
    CREATE_QQ_PROFILE_UPDATED_INDEX_SQL,
)


class QQBoxDBManager:
    """Small async wrapper around the plugin-local SQLite database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._initialized = False

    async def init_db(self) -> None:
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            await asyncio.to_thread(self._init_db_sync)
            self._initialized = True

    def _init_db_sync(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(CREATE_QQ_PROFILE_TABLE_SQL)
            conn.execute(CREATE_QQ_PROFILE_UPDATED_INDEX_SQL)
            conn.execute(CREATE_LAYOUT_PRESET_TABLE_SQL)
            conn.execute(CREATE_LAYOUT_PRESET_ACTIVE_INDEX_SQL)
            conn.execute("PRAGMA optimize")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open a connection with transaction semantics that is always closed.

        `with sqlite3.Connection` only commits/rolls back the transaction; it
        never closes the connection, so every operation must go through here.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    async def fetch_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        await self.init_db()
        return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    def _fetch_all_sync(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._connection() as conn:
            return list(conn.execute(sql, params).fetchall())

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(self._execute_sync, sql, params)

    async def execute_returning_id(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        await self.init_db()
        async with self._write_lock:
            return await asyncio.to_thread(self._execute_returning_id_sync, sql, params)

    def _execute_returning_id_sync(self, sql: str, params: tuple[Any, ...]) -> int:
        with self._connection() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid)

    async def execute_transaction(
        self, statements: list[tuple[str, tuple[Any, ...]]]
    ) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(self._execute_transaction_sync, statements)

    def _execute_transaction_sync(
        self, statements: list[tuple[str, tuple[Any, ...]]]
    ) -> None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sql, params in statements:
                conn.execute(sql, params)

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._connection() as conn:
            conn.execute(sql, params)

    async def execute_many(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(self._execute_many_sync, sql, rows)

    def _execute_many_sync(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        with self._connection() as conn:
            conn.executemany(sql, rows)

    async def replace_all(
        self,
        delete_sql: str,
        insert_sql: str,
        rows: list[tuple[Any, ...]],
    ) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(
                self._replace_all_sync, delete_sql, insert_sql, rows
            )

    def _replace_all_sync(
        self,
        delete_sql: str,
        insert_sql: str,
        rows: list[tuple[Any, ...]],
    ) -> None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(delete_sql)
            if rows:
                conn.executemany(insert_sql, rows)
