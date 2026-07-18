from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .database import QQBoxDBManager
from .tables import (
    ACTIVATE_LAYOUT_PRESET_SQL,
    DEACTIVATE_LAYOUT_PRESETS_SQL,
    DELETE_LAYOUT_PRESET_SQL,
    INSERT_LAYOUT_PRESET_SQL,
    INSERT_MISSING_QQ_PROFILE_SQL,
    PROFILE_FIELDS,
    REPLACE_ALL_QQ_PROFILES_SQL,
    SELECT_ACTIVE_LAYOUT_PRESET_SQL,
    SELECT_ALL_QQ_PROFILES_SQL,
    SELECT_LAYOUT_PRESET_SQL,
    SELECT_LAYOUT_PRESETS_SQL,
    UPDATE_LAYOUT_PRESET_SQL,
    UPSERT_QQ_PROFILE_SQL,
)


class QQProfileRepo:
    """Repository for QQ display profile persistence."""

    def __init__(
        self,
        db_path: str | Path,
        db: QQBoxDBManager | None = None,
    ):
        self.db = db or QQBoxDBManager(db_path)

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

    async def delete_profile(self, qq: str) -> None:
        await self.db.execute("DELETE FROM qq_profile WHERE qq = ?", (str(qq),))

    async def save_all(self, profiles: dict[str, dict[str, Any]]) -> None:
        rows = self._profile_rows(profiles)
        await self.db.replace_all(
            REPLACE_ALL_QQ_PROFILES_SQL, UPSERT_QQ_PROFILE_SQL, rows
        )

    async def save_missing(self, profiles: dict[str, dict[str, Any]]) -> None:
        rows = self._profile_rows(profiles)
        if rows:
            await self.db.execute_many(INSERT_MISSING_QQ_PROFILE_SQL, rows)

    def _profile_rows(
        self, profiles: dict[str, dict[str, Any]]
    ) -> list[tuple[Any, ...]]:
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


class LayoutPresetRepo:
    """CRUD persistence for named bubble layout presets."""

    def __init__(
        self,
        db_path: str | Path,
        db: QQBoxDBManager | None = None,
    ) -> None:
        self.db = db or QQBoxDBManager(db_path)

    async def init_db(self) -> None:
        await self.db.init_db()

    async def list_all(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(SELECT_LAYOUT_PRESETS_SQL)
        return [self._row_to_dict(row) for row in rows]

    async def get(self, preset_id: int) -> dict[str, Any] | None:
        rows = await self.db.fetch_all(SELECT_LAYOUT_PRESET_SQL, (preset_id,))
        return self._row_to_dict(rows[0]) if rows else None

    async def get_active(self) -> dict[str, Any] | None:
        rows = await self.db.fetch_all(SELECT_ACTIVE_LAYOUT_PRESET_SQL)
        return self._row_to_dict(rows[0]) if rows else None

    async def create(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        preset_id = await self.db.execute_returning_id(
            INSERT_LAYOUT_PRESET_SQL,
            (name, self._encode_config(config)),
        )
        preset = await self.get(preset_id)
        assert preset is not None
        return preset

    async def update(
        self,
        preset_id: int,
        name: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        await self.db.execute(
            UPDATE_LAYOUT_PRESET_SQL,
            (name, self._encode_config(config), preset_id),
        )
        return await self.get(preset_id)

    async def delete(self, preset_id: int) -> bool:
        preset = await self.get(preset_id)
        if preset is None:
            return False
        await self.db.execute(DELETE_LAYOUT_PRESET_SQL, (preset_id,))
        return True

    async def activate(self, preset_id: int | None) -> dict[str, Any] | None:
        statements = [(DEACTIVATE_LAYOUT_PRESETS_SQL, ())]
        if preset_id is not None:
            if await self.get(preset_id) is None:
                return None
            statements.append((ACTIVATE_LAYOUT_PRESET_SQL, (preset_id,)))
        await self.db.execute_transaction(statements)
        return await self.get_active()

    @staticmethod
    def _encode_config(config: dict[str, Any]) -> str:
        return json.dumps(config, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "config": json.loads(row["config_json"]),
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
