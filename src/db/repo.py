from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import QQBoxDBManager
from .tables import (
    INSERT_MISSING_QQ_PROFILE_SQL,
    PROFILE_FIELDS,
    SELECT_ALL_QQ_PROFILES_SQL,
    UPSERT_QQ_PROFILE_SQL,
)


class QQProfileRepo:
    """Repository for QQ display profile persistence."""

    def __init__(self, db_path: str | Path):
        self.db = QQBoxDBManager(db_path)

    async def init_db(self) -> None:
        await self.db.init_db()

    async def load_all(self) -> dict[str, dict[str, Any]]:
        rows = await self.db.fetch_all(SELECT_ALL_QQ_PROFILES_SQL)
        return {
            str(row["qq"]): {
                "nickname": row["nickname"],
                "color": row["color"],
                "content": row["content"],
                "notes": row["notes"],
            }
            for row in rows
        }

    async def upsert_profile(self, qq: str, profile: dict[str, Any]) -> None:
        normalized = self._normalize_profile(profile)
        await self.db.execute(
            UPSERT_QQ_PROFILE_SQL,
            (
                str(qq),
                normalized["nickname"],
                normalized["color"],
                normalized["content"],
                normalized["notes"],
            ),
        )

    async def save_all(self, profiles: dict[str, dict[str, Any]]) -> None:
        rows = self._profile_rows(profiles)
        if rows:
            await self.db.execute_many(UPSERT_QQ_PROFILE_SQL, rows)

    async def save_missing(self, profiles: dict[str, dict[str, Any]]) -> None:
        rows = self._profile_rows(profiles)
        if rows:
            await self.db.execute_many(INSERT_MISSING_QQ_PROFILE_SQL, rows)

    def _profile_rows(self, profiles: dict[str, dict[str, Any]]) -> list[tuple[Any, ...]]:
        rows = []
        for qq, profile in profiles.items():
            normalized = self._normalize_profile(profile)
            rows.append(
                (
                    str(qq),
                    normalized["nickname"],
                    normalized["color"],
                    normalized["content"],
                    normalized["notes"],
                )
            )
        return rows

    def _normalize_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        normalized = {field: profile.get(field) for field in PROFILE_FIELDS}
        color = normalized["color"]
        if color is not None:
            try:
                normalized["color"] = int(color)
            except (TypeError, ValueError):
                normalized["color"] = None
        return normalized
