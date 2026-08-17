import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
import httpx
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as BotImage
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, StarTools, register

from .src.db.database import QQBoxDBManager
from .src.db.repo import LayoutPresetRepo, QQProfileRepo
from .src.chat_bubble_generator import (
    PROHIBITED_LINE_END,
    PROHIBITED_LINE_START,
    UNBREAKABLE_PUNCTUATION_PAIRS,
    ChatBubbleGenerator,
    _with_font_snapshot,
)
from .src.constraints import MAX_MESSAGE_TEXT_LENGTH
from .src.font_manager import (
    FontBundle,
    FontConfig,
    FontManager,
    FontPaths,
    FontState,
    format_font_generation_unavailable,
    format_font_status,
)
from .src.layout import (
    DEFAULT_LAYOUT,
    LayoutValidationError,
    color_tuple,
    normalize_layout,
)
from .src.web_pages import QQBoxWebController

MSG_ID_PATTERN = re.compile(r"\[MSG_ID:[^\]]*\]")
# 布局生成器缓存上限（条），超出后按最近使用顺序逐出
LAYOUT_GENERATOR_CACHE_LIMIT = 8
@register("QQbox", "Lishining", "我想要说的,群友都替我说了!", "1.4.14")
class QQbox(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.Config = config
        base_layout = normalize_layout(DEFAULT_LAYOUT)

        # 视觉参数全部由插件页面的布局预设管理；AstrBot 配置仅保留下载设置。
        self.corner_radius = base_layout["bubble"]["corner_radius"]

        # 使用框架提供的插件持久化数据目录
        self.plugin_dir = Path(__file__).resolve().parent
        self.data_dir = Path(StarTools.get_data_dir()).resolve()
        self.avatar_image_path = self.data_dir / "avatars"
        self.db_dir = self.data_dir / "db"
        self.bubble_background_dir = self.data_dir / "bubble_backgrounds"

        font_download = self._config_group("font_download")

        self.bubble_font_path = ""
        self.nickname_font_path = ""
        self.title_font_path = ""
        self.bubble_font_size = base_layout["bubble"]["font_size"]
        self.nickname_font_size = base_layout["nickname"]["font_size"]
        self.title_font_size = base_layout["title"]["font_size"]
        self.bubble_text_color = color_tuple(base_layout["bubble"]["text_color"])
        self.nickname_text_color = color_tuple(base_layout["nickname"]["color"])
        self.title_text_color = color_tuple(base_layout["title"]["color"])

        # 创建必要的目录
        self.avatar_image_path.mkdir(parents=True, exist_ok=True)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.bubble_background_dir.mkdir(parents=True, exist_ok=True)

        # Legacy JSON path is kept only for one-time migration.
        self.qq_data_file = self.data_dir / "qq_data.json"
        self.legacy_qq_data_files = self._get_legacy_qq_data_files()
        self.qq_db_file = self.db_dir / "qqbox.db"
        self.db_manager = QQBoxDBManager(self.qq_db_file)
        self.qq_profile_repo = QQProfileRepo(self.qq_db_file, self.db_manager)
        self.layout_preset_repo = LayoutPresetRepo(self.qq_db_file, self.db_manager)

        logger.debug(f"[qqbox] 使用:{self.qq_db_file}作为持久化数据存储位置")

        # 初始化QQ数据
        self.qq_title_key = {}

        # 默认气泡背景图（持久化，无预设时生效）
        self.default_bubble_background = self._load_default_bubble_background()

        # 初始化气泡生成器
        self.qqbox = ChatBubbleGenerator(
            bubble_font_path=self.bubble_font_path,
            nickname_font_path=self.nickname_font_path,
            title_font_path=self.title_font_path,
            avatar_image_path=self.avatar_image_path,
            bubble_font_size=self.bubble_font_size,
            nickname_font_size=self.nickname_font_size,
            title_font_size=self.title_font_size,
            text_color=self.bubble_text_color,
            nickname_color=self.nickname_text_color,
            title_color=self.title_text_color,
            corner_radius=self.corner_radius,
            bubble_background_image=self.default_bubble_background,
            bubble_background_dir=self.bubble_background_dir,
        )
        self.font_manager = FontManager(
            self.data_dir,
            self.plugin_dir / "resources" / "font_manifest.json",
            FontConfig(
                bubble_path=self.bubble_font_path,
                nickname_path=self.nickname_font_path,
                title_path=self.title_font_path,
                auto_download=bool(font_download.get("auto_download", True)),
            ),
            bubble_size=self.bubble_font_size,
            nickname_size=self.nickname_font_size,
            title_size=self.title_font_size,
            on_ready=self.qqbox.install_font_bundle,
        )

        # 初始化HTTP客户端（异步）
        self.http_client = None
        self._font_paths_logged_on_failure = False
        self.active_layout_preset = None
        self._generator_cache: OrderedDict[str, ChatBubbleGenerator] = OrderedDict()
        self._generator_cache_lock = threading.Lock()
        self.web_controller = QQBoxWebController(self)
        self.web_controller.register(context)

    # 插件函数
    @filter.command_group("qb")
    async def qb(self):
        pass

    @qb.command("echo")
    async def echo(self, event: AstrMessageEvent, qq: str):
        """通过对应qq的设置发送消息 /qb echo [qq] [text]"""
        if not self._fonts_ready():
            self._log_font_not_ready_paths()
            yield event.plain_result(self._font_unavailable_message())
            return
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        text = (
            event.message_str.replace("qb", "", 1)
            .replace(qq, "", 1)
            .replace("echo", "", 1)
            .strip()
        )
        text = self._remove_message_id_markers(text)[:MAX_MESSAGE_TEXT_LENGTH]
        bot = getattr(event, "bot", None)
        info = await self.get_qq_info(qq, bot)
        img_bytes = await asyncio.to_thread(
            self.create_chat_message,
            qq=qq,
            text=text,
            image=None,
            qq_title_key=self.qq_title_key,
            user_info=info,
        )
        yield event.chain_result([BotImage.fromBytes(img_bytes.getvalue())])

    @qb.command("gif")
    async def get_gif(self, event: AstrMessageEvent, qq: str):
        """获取消息链或回复的gif,生成聊天气泡 /qb gif [qq] [图片] 或者 [图片] 回复 /qb gif [qq]"""
        if not self._fonts_ready():
            self._log_font_not_ready_paths()
            yield event.plain_result(self._font_unavailable_message())
            return
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        img_data = await self._get_images(event)
        if not img_data:
            yield event.plain_result("未检测到图片")
            return
        pil_gif = Image.open(BytesIO(img_data))
        if not getattr(pil_gif, "is_animated", False):
            pil_gif.close()
            yield event.plain_result("该图片不是GIF")
            return
        bot = getattr(event, "bot", None)
        info = await self.get_qq_info(qq, bot)
        img_bytes = await asyncio.to_thread(
            self.create_chat_message_by_gif,
            qq=qq,
            text=None,
            image=pil_gif,
            qq_title_key=self.qq_title_key,
            user_info=info,
        )
        pil_gif.close()
        yield event.chain_result([BotImage.fromBytes(img_bytes.getvalue())])

    @qb.command("img")
    async def echo_img(self, event: AstrMessageEvent, qq: str):
        """获取消息链或回复的图片,生成聊天气泡 /qb img [qq] [图片] 或者 [图片] 回复 /qb img [qq]"""
        if not self._fonts_ready():
            self._log_font_not_ready_paths()
            yield event.plain_result(self._font_unavailable_message())
            return
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        bot = getattr(event, "bot", None)
        info = await self.get_qq_info(qq, bot)
        image_bytes = await self._get_images(event)
        if image_bytes is None:
            yield event.plain_result("获取图片失败")
            return
        pil_image = Image.open(BytesIO(image_bytes))
        img_bytes = await asyncio.to_thread(
            self.create_chat_message,
            qq=qq,
            text=None,
            image=pil_image,
            qq_title_key=self.qq_title_key,
            user_info=info,
        )
        pil_image.close()
        yield event.chain_result([BotImage.fromBytes(img_bytes.getvalue())])

    @qb.command("sc")
    async def set_color(self, event: AstrMessageEvent, qq: str, color: int):
        """设置对应qq的头衔颜色(color:1:灰色,2:紫色,3:黄色,4:绿色) /qb sc [qq] [color]"""
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        try:
            color = int(color)
        except (TypeError, ValueError):
            color = None
        if color not in self.qqbox.color_map:
            yield event.plain_result("颜色编号需在 1-4 之间")
            return
        await self.update_qq_title_key(qq, color=color)
        yield event.plain_result(f"设置成功 qq:{qq}, color:{color}")

    @qb.command("st")
    async def set_title(self, event: AstrMessageEvent, qq: str):
        """设置对应qq的头衔文字 /qb st [qq] [title]"""
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        title = (
            event.message_str.replace("qb", "", 1)
            .replace(qq, "", 1)
            .replace("st", "", 1)
            .strip()
        )
        title = self._remove_message_id_markers(title)
        await self.update_qq_title_key(qq, content=title)
        yield event.plain_result(f"设置成功 qq:{qq}, title:{title}")

    @qb.command("sn")
    async def set_note(self, event: AstrMessageEvent, qq: str):
        """设置对应qq的名字 /qb sn [qq] [note]"""
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        note = (
            event.message_str.replace("qb", "", 1)
            .replace(qq, "", 1)
            .replace("sn", "", 1)
            .strip()
        )
        note = self._remove_message_id_markers(note)
        await self.update_qq_title_key(qq, notes=note)
        yield event.plain_result(f"设置成功 qq:{qq}, note:{note}")

    @qb.command("ua")
    async def update_avatar(self, event: AstrMessageEvent, qq: str):
        """更新qq头像 /qb ua [qq]"""
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        bot = getattr(event, "bot", None)
        try:
            info = await self.get_qq_info(qq, bot, force_refresh=True)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return
        display_name = info.get("name") or qq
        yield event.plain_result(f"更新qq头像 qq:{qq}, nickname:{display_name}")

    @qb.command("font")
    async def font(self, event: AstrMessageEvent, action: str = "status"):
        """查看字体状态或重试下载：/qb font status|retry"""
        action = action.strip().lower()
        if action == "retry":
            if (
                self.font_manager.status().state is FontState.READY
                and not self.font_manager.needs_update
            ):
                yield event.plain_result(
                    "字体已准备完成\n\n"
                    "无需重复下载，现在可以直接使用生图命令。"
                )
                return
            self.font_manager.retry()
            yield event.plain_result(
                "已重新启动字体下载\n\n"
                "下载会在后台继续，不会阻塞其他命令。\n"
                "查看进度：/qb font status"
            )
            return
        if action != "status":
            yield event.plain_result(
                "字体命令使用方法\n\n"
                "查看状态：/qb font status\n"
                "重新下载：/qb font retry"
            )
            return
        yield event.plain_result(self._format_font_status())

    @qb.command("help")
    async def get_help(self, event: AstrMessageEvent):
        """获取帮助 /qb help [qq]"""
        help_text = """QQbox 插件使用说明
1. 生成聊天气泡
   命令：/qb echo [QQ号] [消息内容]
   说明：生成指定QQ用户发送消息的气泡图片，长文本会自动换行
2. 设置头衔颜色
   命令：/qb sc [QQ号] [颜色编号]
   说明：设置用户的头衔气泡背景颜色
   颜色编号：
   1 - 灰色（默认）
   2 - 紫色
   3 - 黄色
   4 - 绿色
3. 设置头衔内容
   命令：/qb st [QQ号] [头衔文字]
   说明：设置用户显示的头衔内容
4. 设置备注名
   命令：/qb sn [QQ号] [备注名]
   说明：设置用户的显示备注名（会覆盖原昵称）
5. 更新qq头像
   命令：/qb ua [QQ号]
   说明：更新qq头像
6. 查看或重试字体下载
   命令：/qb font status 或 /qb font retry
注意：所有QQ号都必须是纯数字格式"""
        yield event.plain_result(help_text)

    # 生命周期管理
    # 启动插件时
    async def initialize(self):
        """异步初始化，创建HTTP客户端"""
        self._log_runtime_paths()
        # 创建异步HTTP客户端
        self.http_client = httpx.AsyncClient(timeout=30.0)
        await self.qq_profile_repo.init_db()
        await self._migrate_legacy_qq_data()
        self.qq_title_key = await self._load_qq_data()
        await self._load_active_layout_preset()
        self.font_manager.start()
        logger.info("QQbox 插件初始化完成")

    # 关闭插件时
    async def terminate(self):
        """清理资源"""
        await self.font_manager.close()
        # 保存QQ数据
        await self._save_qq_data()

        # 关闭HTTP客户端
        if self.http_client:
            await self.http_client.aclose()
            logger.info("HTTP客户端已关闭")

    # 工具方法
    # 保存QQ数据
    async def _save_qq_data(self):
        """保存QQ数据到数据库"""
        try:
            await self.qq_profile_repo.save_all(self.qq_title_key)
        except (OSError, sqlite3.DatabaseError) as e:
            logger.error(f"保存QQ数据失败: {e}")

    async def _save_qq_profile(self, qq):
        try:
            await self.qq_profile_repo.upsert_profile(qq, self.qq_title_key[qq])
        except (OSError, sqlite3.DatabaseError) as e:
            logger.error(f"保存QQ数据失败: {qq}: {e}")

    # 获取qq数据
    async def _load_qq_data(self):
        """异步从数据库加载QQ数据"""
        try:
            return await self.qq_profile_repo.load_all()
        except (OSError, sqlite3.DatabaseError) as e:
            logger.error(f"加载QQ数据失败: {e}")
            return {}

    async def _migrate_legacy_qq_data(self):
        """Import old JSON persistence into the SQLite database once."""
        legacy_data = self._load_legacy_qq_data()
        if not legacy_data:
            return

        try:
            await self.qq_profile_repo.save_missing(legacy_data)
        except (OSError, sqlite3.DatabaseError) as e:
            logger.error(f"迁移旧QQ数据失败，已保留旧JSON数据: {e}")
            return

        for legacy_path in self.legacy_qq_data_files:
            if not legacy_path.exists():
                continue
            try:
                legacy_path.unlink()
                logger.info(f"[qqbox] 已迁移并删除旧JSON数据: {legacy_path}")
            except OSError as e:
                logger.error(f"删除旧QQ数据失败: {legacy_path}: {e}")

    def _load_legacy_qq_data(self):
        legacy_data = {}
        for legacy_path in self.legacy_qq_data_files:
            legacy_data.update(self._load_legacy_qq_data_file(legacy_path))
        return legacy_data

    def _load_legacy_qq_data_file(self, legacy_path):
        try:
            if not legacy_path.exists():
                return {}
            content = legacy_path.read_text(encoding="utf-8")
            if not content.strip():
                return {}
            data = json.loads(content)
            if isinstance(data, dict):
                return {
                    str(qq): profile
                    for qq, profile in data.items()
                    if isinstance(profile, dict)
                }
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"加载旧QQ数据失败: {legacy_path}: {e}")
            return {}

    def _get_legacy_qq_data_files(self):
        paths = [
            self.qq_data_file,
            self.data_dir / "avatars" / "qq_data.json",
        ]
        configured_avatar_path = self.Config.get("avatar_image_path", "")
        if configured_avatar_path:
            configured_path = Path(configured_avatar_path)
            if not configured_path.is_absolute():
                configured_path = self.data_dir / configured_path
            paths.append(configured_path / "qq_data.json")

        seen = set()
        unique_paths = []
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique_paths.append(path)
        return unique_paths

    def _log_runtime_paths(self, level="info"):
        log = getattr(logger, level)
        log(f"[qqbox] 持久化数据目录: {self.data_dir}")
        log(f"[qqbox] 头像缓存目录: {self.avatar_image_path}")
        log(f"[qqbox] 数据库路径: {self.qq_db_file}")
        log(f"[qqbox] 旧JSON迁移检测路径: {self.qq_data_file}")
        log(f"[qqbox] 字体持久化目录: {self.font_manager.font_root}")

    def _config_group(self, key):
        value = self.Config.get(key, {})
        return value if isinstance(value, dict) else {}

    def _load_default_bubble_background(self) -> str:
        """读取默认气泡背景图（无预设时生效）。"""
        path = self.data_dir / "default_bubble_background.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return ""
            name = str(data.get("background_image", "") or "").strip()
            return name if name in self.available_background_images() else ""
        except (OSError, json.JSONDecodeError):
            return ""

    def _save_default_bubble_background(self, background_image: str) -> None:
        """保存默认气泡背景图。"""
        if (
            background_image
            and background_image not in self.available_background_images()
        ):
            raise ValueError("背景图不存在")
        path = self.data_dir / "default_bubble_background.json"
        path.write_text(
            json.dumps({"background_image": background_image}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _log_font_not_ready_paths(self):
        if getattr(self, "_font_paths_logged_on_failure", False):
            return
        self._font_paths_logged_on_failure = True
        logger.warning("[qqbox] 字体未加载，打印运行路径用于排查")
        self._log_runtime_paths(level="warning")

    def _format_font_status(self):
        return format_font_status(self.font_manager.status())

    def _font_unavailable_message(self):
        return format_font_generation_unavailable(self.font_manager.status())

    def _fonts_ready(self) -> bool:
        """判断是否有可用字体：默认字体，或激活预设设定的字体。"""
        if self.qqbox.is_load_fonts:
            return True
        active = getattr(self, "active_layout_preset", None)
        if not active:
            return False
        try:
            self._build_layout_generator_cached(active["config"])
            return True
        except Exception:
            return False

    # 检测qq号是否合法
    def _validate_qq(self, qq):
        """验证QQ号是否合法（只包含数字）"""
        if not qq or not isinstance(qq, str):
            return False
        # 只允许数字，防止路径遍历攻击
        if not qq.isdigit():
            logger.warning(f"检测到非法QQ号格式: {qq}")
            return False
        return True

    def _cached_avatar_path(self, qq: str) -> Path | None:
        """Return the effective cached avatar, preferring a user upload."""
        custom = self.avatar_image_path / f"custom-{qq}.png"
        if custom.is_file():
            return custom
        return next(iter(sorted(self.avatar_image_path.glob(f"{qq}-*.png"))), None)

    @staticmethod
    def _remove_message_id_markers(text):
        """移除消息正文中形如 [MSG_ID:...] 的内部标记。"""
        return MSG_ID_PATTERN.sub("", text).strip()

    # 获取qq信息
    async def get_qq_info(self, qq, bot=None, force_refresh: bool = False):
        # 确保头像保存目录存在
        os.makedirs(self.avatar_image_path, exist_ok=True)

        nickname = self.qq_title_key.get(qq, {}).get("nickname", None)

        if force_refresh or nickname is None:
            nickname = await self.get_nickname_by_onebot(qq, bot)
            if nickname:
                await self.update_qq_title_key(qq=qq, nickname=nickname)

        avatar_dir = Path(self.avatar_image_path)

        # 优先使用用户自定义头像（独立于 qlogo 下载，不会被子 /qb ua 覆盖）
        custom_avatar = avatar_dir / f"custom-{qq}.png"
        if custom_avatar.is_file() and not force_refresh:
            return {
                "qq": qq,
                "name": nickname or qq,
                "avatar_path": str(custom_avatar),
            }

        # [兼容] 先检查缓存（qlogo 下载的头像）
        for filename in os.listdir(self.avatar_image_path):
            if (
                filename.startswith(f"{qq}-")
                and filename.endswith(".png")
                and not force_refresh
            ):
                # [兼容]通过老方法获取名称数据
                if nickname is None:
                    nickname = filename[len(f"{qq}-") : -4]
                    if nickname:
                        await self.update_qq_title_key(qq=qq, nickname=nickname)
                    else:
                        nickname = qq
                        await self.update_qq_title_key(qq=qq, nickname=nickname)

                return {
                    "qq": qq,
                    "name": nickname or qq,
                    "avatar_path": str(avatar_dir / filename),
                }

        # 如果不存在头像文件,进行获取
        if self.http_client is None:
            logger.error("HTTP客户端未初始化")
            return {"qq": qq, "name": nickname or qq, "avatar_path": None}

        if force_refresh or nickname is None:
            nickname = await self.get_nickname_by_api(qq, self.http_client)
            if nickname:
                await self.update_qq_title_key(qq=qq, nickname=nickname)

        # 下载头像
        if force_refresh:
            for old_avatar in avatar_dir.glob(f"{qq}-*.png"):
                try:
                    old_avatar.unlink()
                except OSError as exc:
                    logger.warning(f"删除旧头像失败 {old_avatar}: {exc}")

        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
        save_path = avatar_dir / f"{qq}-.png"
        success = await self.download_circular_avatar(
            avatar_url, str(save_path), self.http_client
        )

        if not success:
            if force_refresh:
                raise RuntimeError(f"下载头像失败: {qq}")
            logger.warning(f"头像下载失败，使用默认头像: {qq}")
            return {"qq": qq, "name": nickname or qq, "avatar_path": None}

        return {"qq": qq, "name": nickname or qq, "avatar_path": str(save_path)}

    # 更新self.qq_title_key
    async def update_qq_title_key(
        self, qq, nickname=None, color=None, content=None, notes=None
    ):
        qq_title = self.qq_title_key.get(qq, {})
        self.qq_title_key[qq] = {
            "nickname": nickname
            if nickname is not None
            else qq_title.get("nickname", None),
            "color": color if color is not None else qq_title.get("color", None),
            "content": content
            if content is not None
            else qq_title.get("content", None),
            "notes": notes if notes is not None else qq_title.get("notes", None),
        }
        await self._save_qq_profile(qq)

    async def _load_active_layout_preset(self):
        preset = await self.layout_preset_repo.get_active()
        if preset is None:
            self.active_layout_preset = None
            return
        try:
            preset["config"] = normalize_layout(preset["config"])
            self._validate_layout_fonts(preset["config"])
        except (LayoutValidationError, RuntimeError) as exc:
            logger.error(f"[qqbox] 加载当前布局预设失败: {exc}")
            self.active_layout_preset = None
            return
        self.active_layout_preset = preset

    def set_active_layout_preset(self, preset):
        self.active_layout_preset = preset

    @staticmethod
    def _color_hex(value) -> str:
        channels = tuple(value)
        if len(channels) == 3:
            channels += (255,)
        return "#" + "".join(f"{channel:02X}" for channel in channels[:4])

    def default_layout_config(self):
        """Return the base generator layout without freezing its dynamic rules."""
        layout = normalize_layout(DEFAULT_LAYOUT)
        generator = self.qqbox
        bubble_x, bubble_y = generator.bubble_position
        avatar_x, avatar_y = generator.avatar_position
        layout["canvas"].update(
            {
                "auto_size": True,
                "background_color": self._color_hex(generator.background_color),
                "margin": generator.margin,
            }
        )
        layout["bubble"].update(
            {
                "x": bubble_x,
                "y": bubble_y,
                "padding": generator.bubble_padding,
                "corner_radius": generator.corner_radius,
                "max_width": generator.max_width,
                "background_color": self._color_hex(generator.bubble_bg_color),
                "background_image": generator.bubble_background_image,
                "text_color": self._color_hex(generator.text_color),
                "font_size": generator._font_configs["bubble"][1],
            }
        )
        layout["avatar"].update(
            {
                "x": avatar_x,
                "y": avatar_y,
                "width": generator.avatar_size[0],
                "height": generator.avatar_size[1],
            }
        )
        layout["title"].update(
            {
                "x": bubble_x,
                "y": avatar_y + generator.title_bubble_offset,
                "auto_position": True,
                "padding_x": generator.title_padding_x,
                "padding_y": generator.title_padding_y,
                "font_size": generator._font_configs["title"][1],
                "color": self._color_hex(generator.title_color),
            }
        )
        layout["nickname"].update(
            {
                "auto_position": True,
                "font_size": generator._font_configs["nickname"][1],
                "color": self._color_hex(generator.nickname_color),
            }
        )
        return layout

    def available_font_files(self) -> dict[str, Path]:
        """Return safe font IDs mapped to files available to Page presets."""
        available: dict[str, Path] = {}
        bundle = self.qqbox._current_font_bundle()
        bundle_paths = getattr(bundle, "paths", None)
        if bundle_paths is not None:
            for role in ("bubble", "nickname", "title"):
                path = Path(getattr(bundle_paths, role)).resolve()
                if path.is_file():
                    available[f"current-{role}"] = path

        root = self.font_manager.font_root.resolve()
        if root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {
                    ".ttf",
                    ".ttc",
                    ".otf",
                }:
                    continue
                resolved = path.resolve()
                try:
                    relative = resolved.relative_to(root)
                except ValueError:
                    continue
                if ".staging" in relative.parts:
                    continue
                available[relative.as_posix()] = resolved
        return available

    def available_background_images(self) -> list[str]:
        """返回可用的气泡背景图文件名列表。"""
        root = self.bubble_background_dir
        if not root.is_dir():
            return []
        result = []
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            }:
                result.append(path.name)
        return sorted(result)

    def _validate_layout_fonts(self, layout):
        available = self.available_font_files()
        for role in ("bubble", "nickname", "title"):
            font_id = layout[role]["font"]
            if font_id and font_id not in available:
                raise LayoutValidationError(f"{role}.font 指定的字体不存在")
        bg_id = layout["bubble"].get("background_image")
        if bg_id and bg_id not in self.available_background_images():
            raise LayoutValidationError("bubble.background_image 指定的背景图不存在")

    @staticmethod
    def _layout_font_path(layout, role, available):
        font_id = layout[role]["font"]
        if font_id:
            return available[font_id]
        current = available.get(f"current-{role}")
        if current is None:
            raise RuntimeError(f"{role} 字体尚未准备")
        return current

    def _build_layout_generator(self, raw_layout):
        layout = normalize_layout(raw_layout)
        self._validate_layout_fonts(layout)
        available = self.available_font_files()
        paths = FontPaths(
            bubble=self._layout_font_path(layout, "bubble", available),
            nickname=self._layout_font_path(layout, "nickname", available),
            title=self._layout_font_path(layout, "title", available),
        )
        scale = self.qqbox.SCALE
        bundle = FontBundle(
            bubble=ImageFont.truetype(
                str(paths.bubble), layout["bubble"]["font_size"] * scale
            ),
            nickname=ImageFont.truetype(
                str(paths.nickname), layout["nickname"]["font_size"]
            ),
            nickname_scaled=ImageFont.truetype(
                str(paths.nickname), layout["nickname"]["font_size"] * scale
            ),
            title=ImageFont.truetype(str(paths.title), layout["title"]["font_size"]),
            title_scaled=ImageFont.truetype(
                str(paths.title), layout["title"]["font_size"] * scale
            ),
            paths=paths,
            version="layout-preset",
        )
        # 布局未指定背景图时，回退到插件页面设置的默认气泡背景
        background_image = layout["bubble"]["background_image"] or self.default_bubble_background
        generator = ChatBubbleGenerator(
            bubble_font_path=str(paths.bubble),
            nickname_font_path=str(paths.nickname),
            title_font_path=str(paths.title),
            avatar_image_path=self.avatar_image_path,
            bubble_font_size=layout["bubble"]["font_size"],
            nickname_font_size=layout["nickname"]["font_size"],
            title_font_size=layout["title"]["font_size"],
            bubble_padding=layout["bubble"]["padding"],
            title_padding_x=layout["title"]["padding_x"],
            title_padding_y=layout["title"]["padding_y"],
            bubble_bg_color=color_tuple(layout["bubble"]["background_color"]),
            text_color=color_tuple(layout["bubble"]["text_color"]),
            nickname_color=color_tuple(layout["nickname"]["color"]),
            title_color=color_tuple(layout["title"]["color"]),
            corner_radius=layout["bubble"]["corner_radius"],
            avatar_size=(layout["avatar"]["width"], layout["avatar"]["height"]),
            margin=layout["canvas"]["margin"],
            max_width=layout["bubble"]["max_width"],
            bubble_position=(layout["bubble"]["x"], layout["bubble"]["y"]),
            avatar_position=(layout["avatar"]["x"], layout["avatar"]["y"]),
            title_position=(
                None
                if layout["title"]["auto_position"]
                else (layout["title"]["x"], layout["title"]["y"])
            ),
            nickname_position=(
                None
                if layout["nickname"]["auto_position"]
                else (layout["nickname"]["x"], layout["nickname"]["y"])
            ),
            canvas_size=(
                None
                if layout["canvas"]["auto_size"]
                else (layout["canvas"]["width"], layout["canvas"]["height"])
            ),
            background_color=layout["canvas"]["background_color"],
            bubble_background_image=background_image,
            bubble_background_dir=self.bubble_background_dir,
        )
        generator.install_font_bundle(bundle)
        return generator

    def _layout_generator_cache_key(self, layout) -> str:
        """缓存键：布局内容 + 当前字体快照，任一侧变化都会得到新键。"""
        normalized = normalize_layout(layout)
        bundle = self.qqbox._current_font_bundle()
        paths = getattr(bundle, "paths", None)
        font_state = (
            getattr(bundle, "version", None),
            str(getattr(paths, "bubble", "")),
            str(getattr(paths, "nickname", "")),
            str(getattr(paths, "title", "")),
        )
        payload = json.dumps(
            {
                "layout": normalized,
                "default_background": self.default_bubble_background,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(repr((font_state, payload)).encode("utf-8")).hexdigest()

    def _build_layout_generator_cached(self, raw_layout):
        """按缓存键复用布局生成器，避免每次渲染重复加载字体文件。

        字体重装会更换 bundle 版本与路径，自然得到新缓存键；缓存有界，
        超出 LAYOUT_GENERATOR_CACHE_LIMIT 后按最近使用顺序逐出。
        """
        key = self._layout_generator_cache_key(raw_layout)
        cache = self._generator_cache
        with self._generator_cache_lock:
            cached = cache.get(key)
            if cached is not None:
                cache.move_to_end(key)
                return cached
        generator = self._build_layout_generator(raw_layout)
        with self._generator_cache_lock:
            existing = cache.get(key)
            if existing is not None:
                cache.move_to_end(key)
                return existing
            cache[key] = generator
            while len(cache) > LAYOUT_GENERATOR_CACHE_LIMIT:
                cache.popitem(last=False)
        return generator

    def _active_generator(self):
        active_layout_preset = getattr(self, "active_layout_preset", None)
        if not active_layout_preset:
            return self.qqbox
        return self._build_layout_generator_cached(active_layout_preset["config"])

    def create_chat_message(self, **kwargs):
        return self._active_generator().create_chat_message(**kwargs)

    def create_chat_message_by_gif(self, **kwargs):
        return self._active_generator().create_chat_message_by_gif(**kwargs)

    def _preview_render_context(self, generator, payload):
        qq = str(payload.get("qq") or "10001")
        profile = self.qq_title_key.get(qq, {})
        display_name = str(
            payload.get("display_name")
            or profile.get("notes")
            or profile.get("nickname")
            or "预览用户"
        )[:64]
        title = str(payload.get("title") or profile.get("content") or "示例头衔")[:64]
        try:
            color = int(payload.get("color", profile.get("color") or 4))
        except (TypeError, ValueError):
            color = 4
        if color not in generator.color_map:
            color = 4
        text = str(
            payload.get("text") or "这是一条可实时调整布局的示例气泡。"
        )[:MAX_MESSAGE_TEXT_LENGTH]
        avatar_path = self._cached_avatar_path(qq)
        return qq, display_name, title, color, text, avatar_path

    def render_layout_preview_details(self, layout, payload):
        generator = self._build_layout_generator_cached(layout)
        qq, display_name, title, color, text, avatar_path = (
            self._preview_render_context(generator, payload)
        )
        result = generator.create_chat_message(
            qq=qq,
            text=text,
            image=None,
            qq_title_key={
                qq: {"notes": display_name, "content": title, "color": color}
            },
            user_info={
                "name": display_name,
                "avatar_path": str(avatar_path) if avatar_path else None,
            },
        )
        title_color = generator.color_map[color]
        title_bubble = generator.create_title_bubble(title, title_color)
        title_position = generator.title_position or (
            generator.bubble_position[0],
            generator.avatar_position[1] + generator.title_bubble_offset,
        )
        nickname_position = generator.nickname_position or (
            title_position[0] + title_bubble.width + generator.title_bubble_name_gap,
            generator._centered_nickname_y(
                title_bubble.height, display_name, title_position[1]
            ),
        )
        nickname_bbox = generator.nickname_font.getbbox(display_name)
        bubble = generator.create_chat_bubble(text)
        with Image.open(result) as image:
            canvas_size = image.size
        return result, {
            "canvas": {"width": canvas_size[0], "height": canvas_size[1]},
            "avatar": {
                "x": generator.avatar_position[0],
                "y": generator.avatar_position[1],
                "width": generator.avatar_size[0],
                "height": generator.avatar_size[1],
            },
            "title": {
                "x": title_position[0],
                "y": title_position[1],
                "width": title_bubble.width,
                "height": title_bubble.height,
            },
            "nickname": {
                "x": nickname_position[0],
                "y": nickname_position[1],
                "width": nickname_bbox[2] - nickname_bbox[0],
                "height": nickname_bbox[3] - nickname_bbox[1],
            },
            "bubble": {
                "x": generator.bubble_position[0],
                "y": generator.bubble_position[1],
                "width": bubble.width,
                "height": bubble.height,
            },
        }

    # 通过onebot获取nickname
    async def get_nickname_by_onebot(self, qq, bot=None):
        if bot is None:
            return None
        else:
            try:
                payloads = {"user_id": int(qq), "no_cache": True}
                qq_info = await bot.api.call_action("get_stranger_info", **payloads)
                return qq_info.get("nick", None)
            except Exception:
                logger.error("通过onebot获取nick失败")

    # 通过api获取nickname
    async def get_nickname_by_api(self, qq, http_client):
        # 备用API列表
        apis = [
            f"https://uapis.cn/api/v1/social/qq/userinfo?qq={qq}",
            f"https://api.mmp.cc/api/qqname?qq={qq}",
            f"https://api.uomg.com/api/qq.info?qq={qq}",
        ]

        nickname = qq  # 如果API访问失败,使用qq当默认值,让用户使用提供的备注接口修改名称

        # 尝试多个API
        for api_url in apis:
            try:
                response = await http_client.get(api_url, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    # 尝试解析不同API的响应格式
                    if "data" in data and "name" in data["data"]:
                        nickname = data["data"]["name"]
                        break
                    elif "name" in data:
                        nickname = data["name"]
                        break
                    elif "nickname" in data:
                        nickname = data["nickname"]
                        break
            except Exception as e:
                logger.debug(f"API请求失败 {api_url}: {e}")
                continue
        return nickname

    # 下载并裁剪头像为圆形
    async def download_circular_avatar(
        self, url, save_path, http_client=None, size=None
    ):
        """异步下载并裁剪头像为圆形"""
        if http_client is None:
            logger.error("HTTP客户端未初始化")
            return False

        try:
            response = await http_client.get(url, timeout=15.0)
            response.raise_for_status()

            # 加载图片
            img_data = response.content
            img = Image.open(BytesIO(img_data)).convert("RGBA")

            # 创建圆形头像
            result = self.create_circular_avatar(img, size)

            # 保存头像
            result.save(save_path)
            logger.debug(f"头像已保存: {save_path}")
            return True

        except httpx.RequestError as e:
            logger.error(f"下载头像请求失败: {e}")
        except Exception as e:
            logger.error(f"处理头像失败: {e}")

        return False

    # 将图片裁剪为圆形
    def create_circular_avatar(self, img, size=None):
        """将图片裁剪为圆形"""
        # 获取图片尺寸
        w, h = img.size
        side = min(w, h)

        # 中心裁剪为正方形
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

        # 调整大小
        if size is None:
            size = side
        img = img.resize((size, size), Image.Resampling.LANCZOS)

        # 创建圆形遮罩
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size, size), fill=255)

        # 应用遮罩
        result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        result.paste(img, (0, 0), mask)
        return result

    # 获取消息里面的img
    async def _get_images(self, event: AstrMessageEvent) -> bytes | None:
        """获取图片数据，支持从消息、回复中获取"""
        components = list(getattr(event.message_obj, "message", ()) or ())
        get_images = getattr(event, "get_images", None)
        if callable(get_images):
            components = list(get_images() or ()) + components

        # AstrBot adapters may expose image segments as objects or dictionaries.
        for component in components:
            candidates = (
                component.chain
                if isinstance(component, Reply) and component.chain
                else (component,)
            )
            for candidate in candidates:
                source = self._image_source(candidate)
                if source:
                    return await self._download_image(source)
        return None

    @staticmethod
    def _image_source(component) -> str | None:
        if isinstance(component, BotImage):
            return component.url or component.file
        if isinstance(component, dict) and component.get("type") == "image":
            data = component.get("data")
            data = data if isinstance(data, dict) else {}
            return data.get("url") or data.get("file") or component.get(
                "url"
            ) or component.get("file")
        return None

    # 通过url下载img
    async def _download_image(self, url: str) -> bytes | None:
        """读取 HTTP(S) 图片或 AstrBot 临时目录中的本地图片。"""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            if parsed.scheme == "file":
                file_path = unquote(parsed.path)
                if len(file_path) >= 3 and file_path[0] == "/" and file_path[2] == ":":
                    file_path = file_path[1:]
                local_path = Path(file_path)
                if parsed.netloc:
                    local_path = Path(f"//{parsed.netloc}{local_path}")
            elif (
                len(parsed.scheme) == 1
                and len(url) > 2
                and url[1] == ":"
                and url[2] in ("/", "\\")
            ):
                local_path = Path(url)
            elif parsed.scheme:
                logger.error(f"不支持的图片地址协议: {url}")
                return None
            else:
                local_path = Path(url)

            try:
                if not local_path.is_file():
                    raise FileNotFoundError(local_path)
                return await asyncio.to_thread(local_path.read_bytes)
            except OSError as e:
                logger.error(f"读取本地图片失败: {local_path}, 错误: {e}")
                return None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.read()
        except Exception as e:
            logger.error(f"下载图片失败: {url}, 错误: {e}")
            return None
