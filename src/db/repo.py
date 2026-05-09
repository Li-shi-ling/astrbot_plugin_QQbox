from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import QQBoxDBManager
from .tables import PROFILE_FIELDS, QQ_PROFILE_TABLE


class QQProfileRepo:
    """Repository for QQ display profile persistence."""

    def __init__(self, db_path: str | Path):
        self.db = QQBoxDBManager(db_path)

    async def init_db(self) -> None:
        await self.db.init_db()

    async def load_all(self) -> dict[str, dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"SELECT qq, nickname, color, content, notes FROM {QQ_PROFILE_TABLE}"
        )
        return {
            str(row["qq"]): {
                "nickname": row["nickname"],
                "color": row["color"],
                "content": row["content"],
                "notes": row["notes"],
            }
            for row in rows
        }

    async def is_empty(self) -> bool:
        rows = await self.db.fetch_all(f"SELECT 1 FROM {QQ_PROFILE_TABLE} LIMIT 1")
        return not rows

    async def upsert_profile(self, qq: str, profile: dict[str, Any]) -> None:
        normalized = self._normalize_profile(profile)
        await self.db.execute(
            f"""
            INSERT INTO {QQ_PROFILE_TABLE} (qq, nickname, color, content, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(qq) DO UPDATE SET
                nickname = excluded.nickname,
                color = excluded.color,
                content = excluded.content,
                notes = excluded.notes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(qq),
                normalized["nickname"],
                normalized["color"],
                normalized["content"],
                normalized["notes"],
            ),
        )

    async def save_all(self, profiles: dict[str, dict[str, Any]]) -> None:
        if not profiles:
            return

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

        await self.db.execute_many(
            f"""
            INSERT INTO {QQ_PROFILE_TABLE} (qq, nickname, color, content, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(qq) DO UPDATE SET
                nickname = excluded.nickname,
                color = excluded.color,
                content = excluded.content,
                notes = excluded.notes,
                updated_at = CURRENT_TIMESTAMP
            """,
            rows,
        )

    async def close(self) -> None:
        await self.db.close()

    def _normalize_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        normalized = {field: profile.get(field) for field in PROFILE_FIELDS}
        color = normalized["color"]
        if color is not None:
            try:
                normalized["color"] = int(color)
            except (TypeError, ValueError):
                normalized["color"] = None
        return normalized
