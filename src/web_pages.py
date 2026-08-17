from __future__ import annotations

import asyncio
import base64
import binascii
import sqlite3
import time
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .layout import LayoutValidationError, normalize_layout

PLUGIN_NAME = "astrbot_plugin_QQbox"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
ALLOWED_UPLOAD_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})


def _decode_uploaded_image(image_b64: str) -> Image.Image:
    """Decode a bounded raster upload and return an independent RGBA image."""
    encoded = image_b64.split(",", 1)[-1].strip()
    if not encoded:
        raise ValueError("缺少图片数据")
    # Base64 grows by roughly 4/3. Reject before decoding to cap peak memory.
    if len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 4:
        raise ValueError("图片不能超过 8 MB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片编码无效") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("图片不能超过 8 MB")
    try:
        with Image.open(BytesIO(raw)) as source:
            if source.format not in ALLOWED_UPLOAD_FORMATS:
                raise ValueError("仅支持 PNG、JPG、WEBP 或 GIF 图片")
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("图片尺寸过大，最多允许 1600 万像素")
            source.load()
            return source.convert("RGBA")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("无法识别图片内容") from exc


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
                "admin/profiles/avatar",
                self.get_avatar,
                ["GET"],
                "Get QQ avatar",
            ),
            (
                "admin/profiles/avatar/upload",
                self.upload_avatar,
                ["POST"],
                "Upload QQ avatar",
            ),
            (
                "admin/profiles/avatar/delete",
                self.delete_avatar,
                ["POST"],
                "Delete QQ avatar",
            ),
            (
                "admin/layout/defaults",
                self.layout_defaults,
                ["GET"],
                "Get layout defaults",
            ),
            ("admin/layout/fonts", self.list_fonts, ["GET"], "List layout fonts"),
            (
                "admin/layout/backgrounds",
                self.list_backgrounds,
                ["GET"],
                "List bubble backgrounds",
            ),
            (
                "admin/layout/backgrounds/upload",
                self.upload_background,
                ["POST"],
                "Upload bubble background",
            ),
            (
                "admin/layout/backgrounds/delete",
                self.delete_background,
                ["POST"],
                "Delete bubble background",
            ),
            (
                "admin/layout/backgrounds/options",
                self.background_options,
                ["GET"],
                "List bubble backgrounds with thumbnail previews",
            ),
            (
                "admin/layout/backgrounds/default",
                self.get_default_background,
                ["GET"],
                "Get default bubble background",
            ),
            (
                "admin/layout/backgrounds/default/set",
                self.set_default_background,
                ["POST"],
                "Set default bubble background",
            ),
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

    def _avatar_path_for(self, qq: str):
        """返回当前生效的头像路径：自定义优先，其次 qlogo 下载的头像。"""
        avatar_dir = self.owner.avatar_image_path
        custom = avatar_dir / f"custom-{qq}.png"
        if custom.is_file():
            return custom, True
        downloaded = next(iter(sorted(avatar_dir.glob(f"{qq}-*.png"))), None)
        if downloaded is not None:
            return downloaded, False
        return None, False

    async def get_avatar(self):
        qq = str(request.query.get("qq", "") or "").strip()
        if not self.owner._validate_qq(qq):
            return error_response("QQ 号必须只包含数字")
        path, custom = self._avatar_path_for(qq)
        if path is None:
            return json_response({"avatar": None, "custom": False})
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return json_response({
            "avatar": f"data:image/png;base64,{data}",
            "custom": custom,
        })

    async def upload_avatar(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象")
        qq = str(payload.get("qq") or "").strip()
        if not self.owner._validate_qq(qq):
            return error_response("QQ 号必须只包含数字")
        image_b64 = payload.get("image")
        if not image_b64 or not isinstance(image_b64, str):
            return error_response("缺少图片数据")
        try:
            img = _decode_uploaded_image(image_b64)
        except ValueError as exc:
            return error_response(f"图片数据无效: {exc}")
        try:
            circular = self.owner.create_circular_avatar(img, 640)
        except Exception as exc:
            return error_response(f"头像处理失败: {exc}")
        finally:
            img.close()
        try:
            self.owner.avatar_image_path.mkdir(parents=True, exist_ok=True)
            save_path = self.owner.avatar_image_path / f"custom-{qq}.png"
            circular.save(save_path, format="PNG")
        finally:
            circular.close()
        return json_response({"custom": True, "qq": qq})

    async def delete_avatar(self):
        payload = await request.json(default={})
        qq = str(payload.get("qq") or "").strip() if isinstance(payload, dict) else ""
        if not self.owner._validate_qq(qq):
            return error_response("QQ 号必须只包含数字")
        save_path = self.owner.avatar_image_path / f"custom-{qq}.png"
        removed = False
        if save_path.is_file():
            try:
                save_path.unlink()
                removed = True
            except OSError as exc:
                logger.error(f"[qqbox] 删除自定义头像失败: {exc}")
                return error_response("删除头像失败", status_code=500)
        return json_response({"deleted": removed, "qq": qq})

    async def layout_defaults(self):
        return json_response(
            {
                "layout": self.owner.default_layout_config(),
                "active": self.owner.active_layout_preset,
            }
        )

    async def list_fonts(self):
        fonts = [
            {"id": font_id, "label": path.name}
            for font_id, path in sorted(self.owner.available_font_files().items())
            if not font_id.startswith("current-")
        ]
        return json_response({"fonts": fonts})

    async def list_backgrounds(self):
        return json_response({
            "backgrounds": self.owner.available_background_images()
        })

    @staticmethod
    def _safe_background_name(name: str) -> str:
        """把用户输入清洗成安全文件名（去路径、去扩展名）。"""
        cleaned = "".join(
            ch for ch in name.strip()
            if ch.isalnum() or ch in {"_", "-", " "}
        ).replace(" ", "_")
        return cleaned[:64]

    async def upload_background(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象")
        image_b64 = payload.get("image")
        if not image_b64 or not isinstance(image_b64, str):
            return error_response("缺少图片数据")
        try:
            img = _decode_uploaded_image(image_b64)
        except ValueError as exc:
            return error_response(f"图片数据无效: {exc}")
        safe_name = self._safe_background_name(str(payload.get("name") or ""))
        if not safe_name:
            safe_name = f"bg-{int(time.time())}"
        filename = f"{safe_name}.png"
        self.owner.bubble_background_dir.mkdir(parents=True, exist_ok=True)
        save_path = self.owner.bubble_background_dir / filename
        try:
            img.save(save_path, format="PNG")
        finally:
            img.close()
        return json_response({"name": filename})

    async def delete_background(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            return error_response("背景图名称无效")
        save_path = self.owner.bubble_background_dir / name
        removed = False
        if save_path.is_file():
            try:
                save_path.unlink()
                removed = True
            except OSError as exc:
                logger.error(f"[qqbox] 删除背景图失败: {exc}")
                return error_response("删除背景图失败", status_code=500)
        if removed and self.owner.default_bubble_background == name:
            self.owner.default_bubble_background = ""
            self.owner._save_default_bubble_background("")
            self.owner.qqbox.bubble_background_image = ""
        return json_response({"deleted": removed, "name": name})

    async def background_options(self):
        """Return background images with small base64 thumbnails for the config UI."""
        options = []
        for name in self.owner.available_background_images():
            path = self.owner.bubble_background_dir / name
            try:
                with Image.open(path) as img:
                    img = img.convert("RGBA")
                    img.thumbnail((96, 96), Image.Resampling.LANCZOS)
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                data_url = "data:image/png;base64," + base64.b64encode(
                    buf.getvalue()
                ).decode("ascii")
                options.append({"name": name, "value": name, "image": data_url})
            except OSError as exc:
                logger.warning(f"[qqbox] 读取背景图缩略图失败: {name}: {exc}")
        return json_response({"options": options})

    async def get_default_background(self):
        return json_response({
            "background_image": self.owner.default_bubble_background
        })

    async def set_default_background(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
        if name and name not in self.owner.available_background_images():
            return error_response("背景图不存在")
        self.owner.default_bubble_background = name
        self.owner._save_default_bubble_background(name)
        # 立即更新默认生成器，让无预设的生图命令直接生效
        self.owner.qqbox.bubble_background_image = name
        return json_response({"background_image": name})

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
            preset_id, existing["name"], self.owner.default_layout_config()
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
            result, resolved = await asyncio.to_thread(
                self.owner.render_layout_preview_details, layout, payload
            )
        except (LayoutValidationError, RuntimeError, OSError, ValueError) as exc:
            return error_response(str(exc))
        encoded = base64.b64encode(result.getvalue()).decode("ascii")
        return json_response(
            {"image": f"data:image/png;base64,{encoded}", "resolved": resolved}
        )

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
