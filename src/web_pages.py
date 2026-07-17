from __future__ import annotations

import asyncio
import base64
import sqlite3
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .layout import DEFAULT_LAYOUT, LayoutValidationError, normalize_layout

PLUGIN_NAME = "astrbot_plugin_QQbox"


class QQBoxWebController:
    """Plugin Pages controller for profile and bubble-layout administration."""

    def __init__(self, owner) -> None:
        self.owner = owner

    def register(self, context) -> None:
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            return
        routes = (
            ("admin/profiles", self.list_profiles, ["GET"], "List QQ profiles"),
            ("admin/profiles/save", self.save_profile, ["POST"], "Save QQ profile"),
            (
                "admin/profiles/delete",
                self.delete_profile,
                ["POST"],
                "Delete QQ profile",
            ),
            (
                "admin/layout/defaults",
                self.layout_defaults,
                ["GET"],
                "Get layout defaults",
            ),
            ("admin/layout/fonts", self.list_fonts, ["GET"], "List layout fonts"),
            ("admin/layout/presets", self.list_presets, ["GET"], "List layout presets"),
            (
                "admin/layout/presets/save",
                self.save_preset,
                ["POST"],
                "Save layout preset",
            ),
            (
                "admin/layout/presets/delete",
                self.delete_preset,
                ["POST"],
                "Delete layout preset",
            ),
            (
                "admin/layout/presets/activate",
                self.activate_preset,
                ["POST"],
                "Activate layout preset",
            ),
            (
                "admin/layout/presets/reset",
                self.reset_preset,
                ["POST"],
                "Reset layout preset",
            ),
            ("admin/layout/preview", self.preview, ["POST"], "Render bubble preview"),
        )
        for suffix, handler, methods, description in routes:
            register(f"/{PLUGIN_NAME}/{suffix}", handler, methods, description)

    async def list_profiles(self):
        keyword = str(request.query.get("q", "") or "").strip().casefold()
        rows = []
        for qq, profile in sorted(self.owner.qq_title_key.items()):
            row = self._serialize_profile(qq, profile)
            haystack = " ".join(str(value or "") for value in row.values()).casefold()
            if not keyword or keyword in haystack:
                rows.append(row)
        return json_response({"profiles": rows})

    async def save_profile(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象")
        qq = str(payload.get("qq") or "").strip()
        if not self.owner._validate_qq(qq):
            return error_response("QQ 号必须只包含数字")
        try:
            profile = self._normalize_profile(payload, self.owner.qq_title_key.get(qq))
            await self.owner.qq_profile_repo.upsert_profile(qq, profile)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        except (OSError, sqlite3.DatabaseError) as exc:
            logger.error(f"[qqbox] Page 保存用户失败: {exc}")
            return error_response("数据库写入失败", status_code=500)
        self.owner.qq_title_key[qq] = profile
        return json_response({"profile": self._serialize_profile(qq, profile)})

    async def delete_profile(self):
        payload = await request.json(default={})
        qq = str(payload.get("qq") or "").strip() if isinstance(payload, dict) else ""
        if not self.owner._validate_qq(qq):
            return error_response("QQ 号必须只包含数字")
        try:
            await self.owner.qq_profile_repo.delete_profile(qq)
        except (OSError, sqlite3.DatabaseError) as exc:
            logger.error(f"[qqbox] Page 删除用户失败: {exc}")
            return error_response("数据库写入失败", status_code=500)
        removed = self.owner.qq_title_key.pop(qq, None) is not None
        return json_response({"deleted": removed, "qq": qq})

    async def layout_defaults(self):
        return json_response(
            {
                "layout": normalize_layout(DEFAULT_LAYOUT),
                "active": self.owner.active_layout_preset,
            }
        )

    async def list_fonts(self):
        fonts = [
            {"id": font_id, "label": path.name}
            for font_id, path in sorted(self.owner.available_font_files().items())
        ]
        return json_response({"fonts": fonts})

    async def list_presets(self):
        return json_response(
            {"presets": await self.owner.layout_preset_repo.list_all()}
        )

    async def save_preset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象")
        name = str(payload.get("name") or "").strip()
        if not 1 <= len(name) <= 64:
            return error_response("预设名称长度必须在 1 到 64 个字符之间")
        try:
            layout = normalize_layout(payload.get("config"))
            self.owner._validate_layout_fonts(layout)
            preset_id = self._optional_id(payload.get("id"))
            if preset_id is None:
                preset = await self.owner.layout_preset_repo.create(name, layout)
            else:
                preset = await self.owner.layout_preset_repo.update(
                    preset_id, name, layout
                )
                if preset is None:
                    return error_response("预设不存在", status_code=404)
        except LayoutValidationError as exc:
            return error_response(str(exc))
        except sqlite3.IntegrityError:
            return error_response("预设名称已存在")
        except (OSError, sqlite3.DatabaseError) as exc:
            logger.error(f"[qqbox] Page 保存预设失败: {exc}")
            return error_response("数据库写入失败", status_code=500)
        if preset["is_active"]:
            self.owner.set_active_layout_preset(preset)
        return json_response({"preset": preset})

    async def delete_preset(self):
        payload = await request.json(default={})
        try:
            preset_id = self._required_id(payload)
        except ValueError as exc:
            return error_response(str(exc))
        existing = await self.owner.layout_preset_repo.get(preset_id)
        deleted = await self.owner.layout_preset_repo.delete(preset_id)
        if existing and existing["is_active"]:
            self.owner.set_active_layout_preset(None)
        return json_response({"deleted": deleted, "id": preset_id})

    async def activate_preset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象")
        raw_id = payload.get("id")
        if raw_id in (None, ""):
            await self.owner.layout_preset_repo.activate(None)
            self.owner.set_active_layout_preset(None)
            return json_response({"active": None})
        try:
            preset_id = int(raw_id)
        except (TypeError, ValueError):
            return error_response("预设 ID 无效")
        preset = await self.owner.layout_preset_repo.activate(preset_id)
        if preset is None:
            return error_response("预设不存在", status_code=404)
        self.owner.set_active_layout_preset(preset)
        return json_response({"active": preset})

    async def reset_preset(self):
        payload = await request.json(default={})
        try:
            preset_id = self._required_id(payload)
        except ValueError as exc:
            return error_response(str(exc))
        existing = await self.owner.layout_preset_repo.get(preset_id)
        if existing is None:
            return error_response("预设不存在", status_code=404)
        preset = await self.owner.layout_preset_repo.update(
            preset_id, existing["name"], normalize_layout(DEFAULT_LAYOUT)
        )
        assert preset is not None
        if preset["is_active"]:
            self.owner.set_active_layout_preset(preset)
        return json_response({"preset": preset})

    async def preview(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象")
        try:
            layout = normalize_layout(payload.get("config"))
            self.owner._validate_layout_fonts(layout)
            result = await asyncio.to_thread(
                self.owner.render_layout_preview, layout, payload
            )
        except (LayoutValidationError, RuntimeError, OSError, ValueError) as exc:
            return error_response(str(exc))
        encoded = base64.b64encode(result.getvalue()).decode("ascii")
        return json_response({"image": f"data:image/png;base64,{encoded}"})

    @staticmethod
    def _serialize_profile(qq: str, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "qq": qq,
            "name": profile.get("notes") or profile.get("nickname") or qq,
            "stored_name": profile.get("notes"),
            "nickname": profile.get("nickname"),
            "title": profile.get("content"),
            "color": profile.get("color") or 1,
        }

    @staticmethod
    def _normalize_profile(
        payload: dict[str, Any], existing: dict[str, Any] | None
    ) -> dict[str, Any]:
        existing = existing or {}
        name = str(payload.get("name") or "").strip() or None
        title = str(payload.get("title") or "").strip() or None
        if name and len(name) > 64:
            raise ValueError("用户名称不能超过 64 个字符")
        if title and len(title) > 64:
            raise ValueError("用户头衔不能超过 64 个字符")
        try:
            color = int(payload.get("color", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("用户颜色无效") from exc
        if color not in {1, 2, 3, 4}:
            raise ValueError("用户颜色必须是 1 到 4")
        return {
            "nickname": existing.get("nickname"),
            "notes": name,
            "content": title,
            "color": color,
        }

    @staticmethod
    def _optional_id(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            preset_id = int(value)
        except (TypeError, ValueError) as exc:
            raise LayoutValidationError("预设 ID 无效") from exc
        if preset_id <= 0:
            raise LayoutValidationError("预设 ID 无效")
        return preset_id

    @staticmethod
    def _required_id(payload: Any) -> int:
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        try:
            preset_id = int(payload.get("id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("预设 ID 无效") from exc
        if preset_id <= 0:
            raise ValueError("预设 ID 无效")
        return preset_id
