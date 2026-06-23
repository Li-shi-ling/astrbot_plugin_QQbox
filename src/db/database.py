from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from .tables import CREATE_QQ_PROFILE_TABLE_SQL, CREATE_QQ_PROFILE_UPDATED_INDEX_SQL


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
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(CREATE_QQ_PROFILE_TABLE_SQL)
            conn.execute(CREATE_QQ_PROFILE_UPDATED_INDEX_SQL)
            conn.execute("PRAGMA optimize")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        await self.init_db()
        return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    def _fetch_all_sync(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    async def execute_many(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(self._execute_many_sync, sql, rows)

    def _execute_many_sync(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        with self._connect() as conn:
            conn.executemany(sql, rows)
            conn.commit()

    async def replace_all(
        self,
        delete_sql: str,
        insert_sql: str,
        rows: list[tuple[Any, ...]],
    ) -> None:
        await self.init_db()
        async with self._write_lock:
            await asyncio.to_thread(self._replace_all_sync, delete_sql, insert_sql, rows)

    def _replace_all_sync(
        self,
        delete_sql: str,
        insert_sql: str,
        rows: list[tuple[Any, ...]],
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(delete_sql)
            if rows:
                conn.executemany(insert_sql, rows)
            conn.commit()
