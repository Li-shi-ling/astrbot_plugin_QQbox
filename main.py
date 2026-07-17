import asyncio
import json
import os
import re
import sqlite3
import threading
import unicodedata
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageSequence

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as BotImage
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, StarTools, register

from .src.db.database import QQBoxDBManager
from .src.db.repo import LayoutPresetRepo, QQProfileRepo
from .src.font_manager import (
    FontBundle,
    FontConfig,
    FontManager,
    FontPaths,
    FontState,
)
from .src.layout import (
    DEFAULT_LAYOUT,
    LayoutValidationError,
    color_tuple,
    normalize_layout,
)
from .src.web_pages import QQBoxWebController

MSG_ID_PATTERN = re.compile(r"\[MSG_ID:[^\]]*\]")
# CLReq 6.1.1 "strict" line-start/line-end prohibition subset.  This is a
# deliberately documented Chinese tailoring of UAX #14 rather than a claim of
# implementing every Unicode line-breaking class.
# https://www.w3.org/TR/clreq/#h-prohibition-rules-for-line-start-and-line-end
PROHIBITED_LINE_START = frozenset(
    "、，。．：；！？‼⁇⁈⁉’”）〕】〗〙〛］｝〉》」』々·・—⸺‥…～／,.!?;:/)]}>'\"%"
)
PROHIBITED_LINE_END = frozenset("‘“（〔【〖〘〚［｛〈《「『／([<{/\"'")
UNBREAKABLE_PUNCTUATION_PAIRS = frozenset({"——", "……"})


def _with_font_snapshot(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        bundle = self._font_bundle
        if bundle is None:
            raise RuntimeError("字体尚未准备完成")
        previous = getattr(self._render_font_state, "bundle", None)
        self._render_font_state.bundle = bundle
        try:
            return method(self, *args, **kwargs)
        finally:
            if previous is None:
                del self._render_font_state.bundle
            else:
                self._render_font_state.bundle = previous

    return wrapped


@register("QQbox", "Lishining", "我想要说的,群友都替我说了!", "1.4.4")
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
        )
        self.font_manager = FontManager(
            self.data_dir,
            self.plugin_dir / "resources" / "font_manifest.json",
            FontConfig(
                bubble_path=self.bubble_font_path,
                nickname_path=self.nickname_font_path,
                title_path=self.title_font_path,
                auto_download=bool(font_download.get("auto_download", True)),
                github_mirror=str(font_download.get("github_mirror", "") or ""),
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
        self.web_controller = QQBoxWebController(self)
        self.web_controller.register(context)

    # 插件函数
    @filter.command_group("qb")
    async def qb(self):
        pass

    @qb.command("echo")
    async def echo(self, event: AstrMessageEvent, qq: str):
        """通过对应qq的设置发送消息 /qb echo [qq] [text]"""
        if not self.qqbox.is_load_fonts:
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
        text = self._remove_message_id_markers(text)
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
        """获取消息链或回复的gif,生成聊天气泡 /qb [qq] [图片] 或者 [图片] 回复 /qb [qq]"""
        if not self.qqbox.is_load_fonts:
            self._log_font_not_ready_paths()
            yield event.plain_result(self._font_unavailable_message())
            return
        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("未检测到图片")
            return
        img_data = await self._download_image(img_url)
        if not img_data:
            yield event.plain_result("图片下载失败")
            return
        pil_gif = Image.open(BytesIO(img_data))
        if not getattr(pil_gif, "is_animated", False):
            yield event.plain_result("该图片不是GIF")
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
        yield event.chain_result([BotImage.fromBytes(img_bytes.getvalue())])

    @qb.command("img")
    async def echo_img(self, event: AstrMessageEvent, qq: str):
        """获取消息链或回复的图片,生成聊天气泡 /qb [qq] [图片] 或者 [图片] 回复 /qb [qq]"""
        if not self.qqbox.is_load_fonts:
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
        yield event.chain_result([BotImage.fromBytes(img_bytes.getvalue())])

    @qb.command("sc")
    async def set_color(self, event: AstrMessageEvent, qq: str, color: int):
        """设置对应qq的头衔颜色(color:1:灰色,2:紫色,3:黄色,4:绿色) /qb sc [qq] [color]"""
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
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
                yield event.plain_result("字体已就绪，无需重试")
                return
            self.font_manager.retry()
            yield event.plain_result(
                "已启动字体检查/下载任务，可用 /qb font status 查看进度"
            )
            return
        if action != "status":
            yield event.plain_result("用法：/qb font status 或 /qb font retry")
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

    def _log_font_not_ready_paths(self):
        if getattr(self, "_font_paths_logged_on_failure", False):
            return
        self._font_paths_logged_on_failure = True
        logger.warning("[qqbox] 字体未加载，打印运行路径用于排查")
        self._log_runtime_paths(level="warning")

    def _format_font_status(self):
        status = self.font_manager.status()
        progress = ""
        if status.total > 0:
            progress = f"\n进度：{status.downloaded}/{status.total} bytes"
        error = f"\n错误：{status.error}" if status.error else ""
        return (
            f"字体状态：{status.state.value}\n"
            f"版本：{status.version}\n"
            f"缓存：{status.cache_path}{progress}{error}"
        )

    def _font_unavailable_message(self):
        status = self.font_manager.status()
        if status.state in {
            FontState.CHECKING,
            FontState.DOWNLOADING,
            FontState.VERIFYING,
            FontState.LOADING,
            FontState.NOT_STARTED,
        }:
            return "字体正在后台准备，请稍后重试；可用 /qb font status 查看进度"
        return "字体准备失败，请用 /qb font status 查看原因，或用 /qb font retry 重试"

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

        # [兼容] 先检查缓存
        avatar_dir = Path(self.avatar_image_path)
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
                    "name": nickname,
                    "avatar_path": str(avatar_dir / filename),
                }

        # 如果不存在头像文件,进行获取
        if self.http_client is None:
            logger.error("HTTP客户端未初始化")
            return None

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
                    logger.warning(f"鍒犻櫎鏃уご鍍忓け璐? {old_avatar}: {exc}")

        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
        save_path = avatar_dir / f"{qq}-.png"
        success = await self.download_circular_avatar(
            avatar_url, str(save_path), self.http_client
        )

        if not success:
            raise RuntimeError(f"下载头像失败: {qq}")

        return {"qq": qq, "name": nickname, "avatar_path": str(save_path)}

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

    def _validate_layout_fonts(self, layout):
        available = self.available_font_files()
        for role in ("bubble", "nickname", "title"):
            font_id = layout[role]["font"]
            if font_id and font_id not in available:
                raise LayoutValidationError(f"{role}.font 指定的字体不存在")

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
        )
        generator.install_font_bundle(bundle)
        return generator

    def _active_generator(self):
        active_layout_preset = getattr(self, "active_layout_preset", None)
        if not active_layout_preset:
            return self.qqbox
        return self._build_layout_generator(active_layout_preset["config"])

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
        text = str(payload.get("text") or "这是一条可实时调整布局的示例气泡。")[:500]
        avatar_path = next(self.avatar_image_path.glob(f"{qq}-*.png"), None)
        return qq, display_name, title, color, text, avatar_path

    def render_layout_preview(self, layout, payload):
        generator = self._build_layout_generator(layout)
        qq, display_name, title, color, text, avatar_path = (
            self._preview_render_context(generator, payload)
        )
        return generator.create_chat_message(
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

    def render_layout_preview_details(self, layout, payload):
        generator = self._build_layout_generator(layout)
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
        # 查找直接发送的图片或回复中的图片
        for component in event.message_obj.message:
            if isinstance(component, BotImage):
                if component.url:
                    return await self._download_image(component.url)
                elif component.file:
                    return await self._download_image(component.file)
            elif isinstance(component, Reply) and component.chain:
                for reply_component in component.chain:
                    if isinstance(reply_component, BotImage):
                        if reply_component.url:
                            return await self._download_image(reply_component.url)
                        elif reply_component.file:
                            return await self._download_image(reply_component.file)
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

    # 获取图片url
    def _get_image_url(self, event: AstrMessageEvent) -> str | None:
        if hasattr(event, "get_images"):
            images = event.get_images()
            if images:
                return images[0].url

        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    for item in seg.chain:
                        if isinstance(item, BotImage) and item.url:
                            return item.url
                        if isinstance(item, dict) and item.get("type") == "image":
                            return item.get("data", {}).get("url") or item.get("url")
                if isinstance(seg, dict) and seg.get("type") == "image":
                    return seg.get("data", {}).get("url") or seg.get("url")
                if isinstance(seg, BotImage) and seg.url:
                    return seg.url
        return None


# ------------------------------------------------------------------------------
# 高 DPI 超清聊天气泡生成器
# ------------------------------------------------------------------------------
class ChatBubbleGenerator:
    def __init__(
        self,
        bubble_font_path,
        nickname_font_path,
        title_font_path,
        avatar_image_path,
        bubble_font_size=34,
        nickname_font_size=25,
        title_font_size=19,
        bubble_padding=20,
        title_padding_x=25,
        title_padding_y=15,
        title_bubble_offset=5,
        bubble_bg_color=(255, 255, 255, 220),
        text_color=(0, 0, 0, 255),
        nickname_color=(128, 128, 128, 255),
        title_color=(255, 255, 255, 255),
        corner_radius=27,
        avatar_size=(89, 89),
        margin=20,
        title_bubble_name_gap=8,
        max_width=640,
        bubble_position=(120, 60),
        avatar_position=(23, 10),
        title_position=None,
        nickname_position=None,
        canvas_size=None,
        background_color="#F0F0F2",
    ):
        # 常量配置
        self.SCALE = 4  # supersampling 倍率

        # 字体配置
        self._font_configs = {
            "bubble": (bubble_font_path, bubble_font_size),
            "nickname": (nickname_font_path, nickname_font_size),
            "title": (title_font_path, title_font_size),
        }

        # 颜色配置
        self.color_map = {
            1: (181, 182, 181, 220),  # #B5B6B5
            2: (214, 154, 255, 220),  # #D69AFF
            3: (255, 198, 41, 220),  # #FFC629
            4: (82, 215, 197, 220),  # #52D7C5
        }

        # 缓存
        self._temp_canvas = None
        self._temp_draw = None

        self._font_bundle = None
        self._render_font_state = threading.local()

        # 布局参数
        self.bubble_padding = bubble_padding
        self.title_padding_x = title_padding_x
        self.title_padding_y = title_padding_y
        self.title_bubble_offset = title_bubble_offset
        self.title_bubble_name_gap = title_bubble_name_gap
        self.margin = margin
        self.max_width = max_width
        self.corner_radius = corner_radius
        self.avatar_size = avatar_size
        self.bubble_position = bubble_position
        self.avatar_position = avatar_position
        self.title_position = title_position
        self.nickname_position = nickname_position
        self.canvas_size = canvas_size

        # 样式参数
        self.bubble_bg_color = bubble_bg_color
        self.text_color = text_color
        self.nickname_color = nickname_color
        self.title_color = title_color
        self.avatar_image_path = avatar_image_path

        # 背景颜色处理
        if isinstance(background_color, str) and background_color.startswith("#"):
            background_color = background_color[:7]
            self.background_color = tuple(
                int(background_color[i : i + 2], 16) for i in (1, 3, 5)
            ) + (255,)
        else:
            self.background_color = (240, 240, 242, 255)  # 默认颜色

    # ------------------------------------------------------------------------------
    # 字体管理
    # ------------------------------------------------------------------------------
    @property
    def is_load_fonts(self):
        return self._font_bundle is not None

    @property
    def bubble_font(self):
        bundle = self._current_font_bundle()
        return bundle.bubble if bundle else None

    @property
    def nickname_font(self):
        bundle = self._current_font_bundle()
        return bundle.nickname if bundle else None

    @property
    def nickname_SCALE_font(self):
        bundle = self._current_font_bundle()
        return bundle.nickname_scaled if bundle else None

    @property
    def title_font(self):
        bundle = self._current_font_bundle()
        return bundle.title if bundle else None

    @property
    def title_SCALE_font(self):
        bundle = self._current_font_bundle()
        return bundle.title_scaled if bundle else None

    def install_font_bundle(self, bundle: FontBundle):
        """一次性安装完整字体快照，避免部分字体对渲染可见。"""
        self._font_bundle = bundle

    def _current_font_bundle(self):
        return getattr(self._render_font_state, "bundle", None) or self._font_bundle

    # ------------------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------------------
    def _get_temp_draw(self):
        """获取临时绘图上下文（延迟初始化）"""
        if self._temp_canvas is None:
            self._temp_canvas = Image.new("RGBA", (10, 10))
            self._temp_draw = ImageDraw.Draw(self._temp_canvas)
        return self._temp_draw

    def _wrap_text(self, text, font):
        """按 Unicode 文本单元和中文标点禁则自动换行。"""
        return [line for line, _ in self._wrap_text_layout(text, font)]

    def _wrap_text_layout(self, text, font):
        """返回换行文本及其是否为段落末行，供两端对齐使用。"""
        draw = self._get_temp_draw()
        padding = self.bubble_padding * self.SCALE
        max_width = self.max_width * self.SCALE - padding * 2
        fallback_width = self._font_configs["bubble"][1] * self.SCALE
        layout = []

        for paragraph in text.split("\n"):
            if not paragraph:
                layout.append(("", True))
                continue

            units = self._text_units(paragraph)
            start = 0
            paragraph_lines = []
            while start < len(units):
                fit_end = start
                for end in range(start + 1, len(units) + 1):
                    candidate = "".join(units[start:end])
                    width = self._safe_text_width(draw, candidate, font, fallback_width)
                    if width <= max_width:
                        fit_end = end
                        continue
                    break

                if fit_end == start:
                    fit_end = start + 1
                break_at = self._find_legal_break(units, start, fit_end)
                paragraph_lines.append("".join(units[start:break_at]))
                start = break_at

            layout.extend(
                (line, index == len(paragraph_lines) - 1)
                for index, line in enumerate(paragraph_lines)
            )

        return layout

    @staticmethod
    def _text_units(text):
        """按 UAX #14 LB8a/LB9 保留组合序列，并按 CLReq 保留成对标点。"""
        units = []
        index = 0
        while index < len(text):
            unit = text[index]
            index += 1
            while index < len(text):
                char = text[index]
                codepoint = ord(char)
                if (
                    unicodedata.combining(char)
                    or 0xFE00 <= codepoint <= 0xFE0F
                    or 0xE0100 <= codepoint <= 0xE01EF
                    or 0x1F3FB <= codepoint <= 0x1F3FF
                ):
                    unit += char
                    index += 1
                    continue
                if char == "\u200d" and index + 1 < len(text):
                    unit += char + text[index + 1]
                    index += 2
                    continue
                break
            if units and units[-1] + unit in UNBREAKABLE_PUNCTUATION_PAIRS:
                units[-1] += unit
            else:
                units.append(unit)
        return units

    @staticmethod
    def _find_legal_break(units, start, fit_end):
        """选择宽度边界附近的 CLReq 合法断点，无解时按文本单元应急断行。"""
        if fit_end >= len(units):
            return len(units)

        def is_legal(index):
            return ChatBubbleGenerator._is_legal_line_break(
                units[index - 1], units[index]
            )

        for index in range(fit_end, start, -1):
            if is_legal(index):
                return index

        for index in range(fit_end + 1, len(units) + 1):
            if index == len(units):
                return index
            if is_legal(index):
                return index

        return max(start + 1, fit_end)

    @staticmethod
    def _is_western_word_unit(unit):
        """识别应按单词连续排版的字母和数字，不把汉字归入其中。"""
        char = unit[0]
        return unicodedata.category(char)[0] in {
            "L",
            "N",
        } and unicodedata.east_asian_width(char) not in {"W", "F"}

    @staticmethod
    def _is_legal_line_break(left, right):
        """实现当前 CLReq 中文裁剪规则及西文单词的合法断点判断。"""
        if left[-1] in PROHIBITED_LINE_END or right[0] in PROHIBITED_LINE_START:
            return False
        return not (
            ChatBubbleGenerator._is_western_word_unit(left)
            and ChatBubbleGenerator._is_western_word_unit(right)
        )

    @staticmethod
    def _is_justification_gap(left, right):
        """判断边界能否吸收两端对齐字距，避免拆散西文单词与禁则标点。"""
        if not ChatBubbleGenerator._is_legal_line_break(left, right):
            return False
        if left[-1].isspace():
            return True
        return unicodedata.east_asian_width(left[-1]) in {
            "W",
            "F",
        } or unicodedata.east_asian_width(right[0]) in {"W", "F"}

    def _justification_segments(self, line):
        """按可扩展边界切分文本，同时保留西文、数字和 Unicode 文本单元。"""
        units = self._text_units(line)
        if not units:
            return []

        segments = [units[0]]
        for left, right in zip(units, units[1:]):
            if self._is_justification_gap(left, right):
                segments.append(right)
            else:
                segments[-1] += right
        return segments

    def _draw_text_line(self, draw, position, line, font, fill, target_width=None):
        """绘制单行；指定目标宽度时仅均分可调整间隙，不拉伸字形。"""
        if not line or target_width is None:
            draw.text(position, line, fill=fill, font=font)
            return

        segments = self._justification_segments(line)
        if len(segments) < 2:
            draw.text(position, line, fill=fill, font=font)
            return

        fallback_width = self._font_configs["bubble"][1] * self.SCALE
        widths = [
            self._safe_text_width(draw, segment, font, fallback_width)
            for segment in segments
        ]
        extra_width = target_width - sum(widths)
        if extra_width <= 0:
            draw.text(position, line, fill=fill, font=font)
            return

        gap = extra_width / (len(segments) - 1)
        if gap > fallback_width * 0.5:
            draw.text(position, line, fill=fill, font=font)
            return

        x, y = position
        for index, (segment, width) in enumerate(zip(segments, widths)):
            draw.text((round(x), y), segment, fill=fill, font=font)
            x += width
            if index < len(segments) - 1:
                x += gap

    def _create_rounded_mask(self, width, height):
        """创建圆角遮罩"""
        mask = Image.new("L", (width, height), 0)
        draw_mask = ImageDraw.Draw(mask)

        # 动态计算圆角半径
        min_side = min(width, height)
        dynamic_radius = int(min_side * 0.05)
        final_radius = min(dynamic_radius, 50 * self.SCALE)

        draw_mask.rounded_rectangle(
            (0, 0, width, height), radius=final_radius, fill=255
        )
        return mask

    def _resize_image_for_bubble(self, image, padding=None):
        """调整图片大小以适应气泡"""
        if padding is None:
            padding = self.bubble_padding * self.SCALE

        max_width = self.max_width * self.SCALE - padding * 2
        orig_width, orig_height = image.size

        if orig_width <= max_width:
            return image

        # 按比例缩放
        ratio = max_width / orig_width
        new_width = int(orig_width * ratio)
        new_height = int(orig_height * ratio)

        return image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    def _text_layout_metrics(self, text, font):
        """返回文本换行后的宽高，尺寸均为高 DPI 坐标。"""
        layout = self._wrap_text_layout(text, font) if text else []
        if not layout:
            layout = [("", True)]
        lines = [line for line, _ in layout]
        justify_lines = [not is_paragraph_end for _, is_paragraph_end in layout]

        draw = self._get_temp_draw()
        bbox = font.getbbox("字")
        line_height = bbox[3] - bbox[1] + 4 * self.SCALE
        text_width = max(
            self._safe_text_width(
                draw,
                line,
                font,
                self._font_configs["bubble"][1] * self.SCALE,
            )
            for line in lines
        )
        return (
            lines,
            justify_lines,
            line_height,
            text_width,
            line_height * len(lines),
        )

    # ------------------------------------------------------------------------------
    # 气泡创建方法
    # ------------------------------------------------------------------------------
    def create_chat_bubble(self, text):
        """创建纯文本聊天气泡"""
        SCALE = self.SCALE
        font = self.bubble_font
        padding = self.bubble_padding * SCALE

        lines, justify_lines, line_height, text_width, text_height = (
            self._text_layout_metrics(text, font)
        )
        line_count = len(lines)

        width = int(text_width + padding * 2)
        height = int(text_height + padding * (2 + line_count))

        # 创建画布
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_canvas = ImageDraw.Draw(canvas)

        # 绘制气泡背景
        draw_canvas.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
        )

        # 绘制文本
        y = padding
        for line, justify in zip(lines, justify_lines):
            self._draw_text_line(
                draw_canvas,
                (padding, y),
                line,
                font,
                self.text_color,
                text_width if justify else None,
            )
            y += line_height + padding

        # 缩放到正常尺寸
        return canvas.resize(
            (width // SCALE, height // SCALE), Image.Resampling.LANCZOS
        )

    def create_chat_img_bubble(self, image):
        """创建纯图片聊天气泡"""
        SCALE = self.SCALE

        # 加载图片
        if isinstance(image, str):
            img = Image.open(image)
        else:
            img = image

        # 缩放图片
        # 在qq会被tx压缩图片,所以要先放大图片
        img = self.resize_by_scale(img, 2)
        img = self._resize_image_for_bubble(img)
        width, height = img.size

        # 创建圆角图片
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        mask = self._create_rounded_mask(width, height)
        canvas.paste(img, (0, 0), mask)

        # 缩放到正常尺寸
        if SCALE > 1:
            canvas = canvas.resize(
                (width // SCALE, height // SCALE), Image.Resampling.LANCZOS
            )

        return canvas

    def create_chat_text_img_bubble(self, text, image):
        """创建图文混合聊天气泡"""
        SCALE = self.SCALE
        font = self.bubble_font
        padding = self.bubble_padding * SCALE

        # 处理图片部分
        img_canvas = self.create_chat_img_bubble(image)
        if SCALE > 1:
            img_canvas = img_canvas.resize(
                (img_canvas.width * SCALE, img_canvas.height * SCALE),
                Image.Resampling.LANCZOS,
            )

        # 处理文本部分
        if text:
            lines, justify_lines, line_height, text_width, text_height = (
                self._text_layout_metrics(text, font)
            )
            line_count = len(lines)
        else:
            lines = []
            justify_lines = []
            line_height = text_width = text_height = line_count = 0

        width = int(max(text_width, img_canvas.width) + padding * 2)
        height = int(
            text_height + padding * (2 + line_count) + img_canvas.height + padding
        )

        # 创建最终画布
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_canvas = ImageDraw.Draw(canvas)

        # 绘制气泡背景
        draw_canvas.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
        )

        # 绘制文本
        if lines:
            y = padding
            for line, justify in zip(lines, justify_lines):
                self._draw_text_line(
                    draw_canvas,
                    (padding, y),
                    line,
                    font,
                    self.text_color,
                    text_width if justify else None,
                )
                y += line_height + padding

        # 粘贴图片
        img_x = (width - img_canvas.width) // 2
        img_y = text_height + padding * (2 + line_count if lines else 1)
        canvas.paste(img_canvas, (img_x, img_y), img_canvas)

        # 缩放到正常尺寸
        return canvas.resize(
            (width // SCALE, height // SCALE), Image.Resampling.LANCZOS
        )

    def _measure_title_bubble(self, text):
        """Return the scaled canvas size and glyph box for the current title font."""
        scale = self.SCALE
        font = self.title_SCALE_font
        draw = self._get_temp_draw()
        text_width = int(draw.textlength(text, font=font))
        bbox = font.getbbox(text)
        text_height = bbox[3] - bbox[1] + 4 * scale
        width = int(text_width + self.title_padding_x * 2)
        height = int(text_height + self.title_padding_y * 3)
        return max(scale, width), max(scale, height), bbox

    def create_title_bubble(self, text, bg_color):
        """创建头衔气泡，并按当前字形尺寸实时居中。"""
        scale = self.SCALE
        font = self.title_SCALE_font
        width, height, bbox = self._measure_title_bubble(text)

        # 创建气泡
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_canvas = ImageDraw.Draw(canvas)

        # 绘制背景
        draw_canvas.rounded_rectangle(
            (0, 0, width, height), radius=8 * scale, fill=bg_color
        )

        glyph_width = bbox[2] - bbox[0]
        glyph_height = bbox[3] - bbox[1]
        text_x = round((width - glyph_width) / 2 - bbox[0])
        text_y = round((height - glyph_height) / 2 - bbox[1])
        draw_canvas.text(
            (text_x, text_y),
            text,
            fill=self.title_color,
            font=font,
        )

        # 缩放到正常尺寸
        return canvas.resize(
            (width // scale, height // scale), Image.Resampling.LANCZOS
        )

    # ------------------------------------------------------------------------------
    # 主要接口（保持签名不变）
    # ------------------------------------------------------------------------------
    @_with_font_snapshot
    def create_chat_message(self, qq, text, image, qq_title_key=None, user_info=None):
        if user_info is None:
            raise ValueError("需要提供user_info参数，避免同步HTTP调用")

        # 提取用户信息
        nickname = user_info.get("name", "未知用户")
        avatar_path = user_info.get("avatar_path")

        # 选择合适的气泡类型
        if text and not image:
            bubble = self.create_chat_bubble(text)
        elif image and not text:
            bubble = self.create_chat_img_bubble(image)
        elif text and image:
            bubble = self.create_chat_text_img_bubble(text, image)
        else:
            # 空消息，创建一个最小气泡
            bubble = self.create_chat_bubble(" ")

        # 处理头衔信息
        title_info = None
        if qq_title_key and qq in qq_title_key:
            title_info = qq_title_key[qq]
            # 优先使用备注名
            if title_info.get("notes"):
                nickname = title_info["notes"]

        # 计算布局尺寸
        bg_size = self._calculate_background_size(bubble, nickname, title_info)
        background = self._create_background_canvas(*bg_size)

        # 添加气泡
        background.alpha_composite(bubble, dest=self.bubble_position)

        # 添加头像
        self._add_avatar(background, avatar_path)

        # 添加昵称和头衔
        self._add_name_and_title(background, nickname, title_info)

        # 返回字节流
        img_bytes = BytesIO()
        background.save(img_bytes, format="PNG", optimize=True)
        img_bytes.seek(0)
        return img_bytes

    @_with_font_snapshot
    def create_chat_message_by_gif(
        self, qq, text, image, qq_title_key=None, user_info=None
    ):
        if user_info is None:
            raise ValueError("需要提供user_info参数，避免同步HTTP调用")

        # 提取用户信息
        nickname = user_info.get("name", "未知用户")
        avatar_path = user_info.get("avatar_path")

        # 处理头衔信息
        title_info = None
        if qq_title_key and qq in qq_title_key:
            title_info = qq_title_key[qq]
            if title_info.get("notes"):
                nickname = title_info["notes"]

        # 分离GIF的每一帧
        frames = []
        durations = []

        for frame in ImageSequence.Iterator(image):
            durations.append(max(20, int(frame.info.get("duration", 100))))
            frame_rgba = frame.copy().convert("RGBA")
            frames.append(frame_rgba)

        if not frames:
            raise ValueError("GIF图片没有有效帧")

        # 使用第一帧创建气泡并计算布局
        first_bubble = self.create_chat_img_bubble(frames[0])
        bg_size = self._calculate_background_size(first_bubble, nickname, title_info)

        # 创建静态背景（包含头像、昵称、头衔）
        static_background = self._create_background_canvas(*bg_size)
        self._add_avatar(static_background, avatar_path)
        self._add_name_and_title(static_background, nickname, title_info)

        # 批量处理所有帧
        processed_frames = []
        bubble_pos = self.bubble_position

        for i, frame in enumerate(frames):
            # 创建当前帧的气泡
            bubble = self.create_chat_img_bubble(frame)

            # 创建新的背景
            background = self._create_background_canvas(*bg_size)

            # 先粘贴气泡
            background.alpha_composite(bubble, dest=bubble_pos)

            # 再粘贴静态元素，但避开气泡区域
            # 创建一个与背景相同大小的掩码
            mask = Image.new("L", bg_size, 255)

            # 在气泡区域创建黑色（透明）区域
            bubble_width, bubble_height = bubble.size
            bubble_area = (
                bubble_pos[0],
                bubble_pos[1],
                bubble_pos[0] + bubble_width,
                bubble_pos[1] + bubble_height,
            )

            # 在掩码中挖空气泡区域
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.rectangle(bubble_area, fill=0)

            # 粘贴静态元素（头像、昵称、头衔）
            background.paste(static_background, (0, 0), mask)

            processed_frames.append(background)

        # 创建GIF
        gif_bytes = BytesIO()

        if len(processed_frames) > 1:
            processed_frames[0].save(
                gif_bytes,
                format="GIF",
                save_all=True,
                append_images=processed_frames[1:],
                duration=durations,
                loop=0,
                optimize=True,
                disposal=2,
            )
        else:
            processed_frames[0].save(gif_bytes, format="PNG", optimize=True)

        gif_bytes.seek(0)
        return gif_bytes

    # ------------------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------------------
    def _calculate_background_size(self, bubble, nickname, title_info=None):
        """计算背景画布尺寸"""
        bubble_w, bubble_h = bubble.size

        # 测量文本宽度
        draw = self._get_temp_draw()
        nickname_width = (
            draw.textlength(nickname, font=self.nickname_font) + self.bubble_padding
        )

        # 计算基础宽度
        width_candidates = [
            self.bubble_position[0] + bubble_w + self.margin,
            self.avatar_position[0] + self.avatar_size[0] + self.margin,
            self.bubble_position[0] + nickname_width,
        ]

        # 计算高度
        height_candidates = [
            self.bubble_position[1] + bubble_h + self.margin,
            self.avatar_position[1] + self.avatar_size[1] + self.margin,
        ]

        # 头衔和昵称必须与实际绘制共用同一套实时测量结果。
        if title_info and title_info.get("content"):
            title_width_scaled, title_height_scaled, _ = self._measure_title_bubble(
                title_info["content"]
            )
            title_width = title_width_scaled // self.SCALE
            title_height = title_height_scaled // self.SCALE
            title_x, title_y = self.title_position or (
                self.bubble_position[0],
                self.avatar_position[1] + self.title_bubble_offset,
            )
            name_x = (
                self.nickname_position[0]
                if self.nickname_position
                else title_x + title_width + self.title_bubble_name_gap
            )
            width_candidates.append(name_x + nickname_width)
            title_top = title_y
            nickname_y = (
                self.nickname_position[1]
                if self.nickname_position
                else self._centered_nickname_y(title_height, nickname, title_y)
            )
            nickname_bbox = self.nickname_font.getbbox(nickname)
            width_candidates.append(title_x + title_width + self.margin)
            height_candidates.extend(
                (
                    title_top + title_height + self.margin,
                    nickname_y + nickname_bbox[3] + self.margin,
                )
            )

        if self.nickname_position and not (title_info and title_info.get("content")):
            nickname_bbox = self.nickname_font.getbbox(nickname)
            width_candidates.append(
                self.nickname_position[0]
                + nickname_bbox[2]
                - nickname_bbox[0]
                + self.margin
            )
            height_candidates.append(
                self.nickname_position[1] + nickname_bbox[3] + self.margin
            )

        if self.canvas_size:
            width_candidates.append(self.canvas_size[0])
            height_candidates.append(self.canvas_size[1])

        return int(max(width_candidates)), int(max(height_candidates))

    def _create_background_canvas(self, width, height):
        """创建背景画布"""
        return Image.new("RGBA", (width, height), self.background_color)

    def _add_avatar(self, background, avatar_path):
        """添加头像到背景"""
        try:
            if avatar_path and os.path.exists(avatar_path):
                avatar = Image.open(avatar_path).convert("RGBA")
                avatar = avatar.resize(self.avatar_size, Image.Resampling.LANCZOS)
                mask_scale = self.SCALE
                mask_size = (
                    self.avatar_size[0] * mask_scale,
                    self.avatar_size[1] * mask_scale,
                )
                circular_mask = Image.new("L", mask_size, 0)
                ImageDraw.Draw(circular_mask).ellipse(
                    (0, 0, mask_size[0] - 1, mask_size[1] - 1), fill=255
                )
                circular_mask = circular_mask.resize(
                    self.avatar_size, Image.Resampling.LANCZOS
                )
                avatar.putalpha(
                    ImageChops.multiply(avatar.getchannel("A"), circular_mask)
                )
                background.alpha_composite(avatar, dest=self.avatar_position)
            else:
                self._create_default_avatar(background)
        except Exception as e:
            logger.error(f"加载头像失败: {e}")
            self._create_default_avatar(background)

    def _create_default_avatar(self, background):
        """创建默认头像"""
        default_avatar = Image.new("RGBA", self.avatar_size, (200, 200, 200, 255))
        background.paste(default_avatar, self.avatar_position)

    def _add_name_and_title(self, background, nickname, title_info=None):
        """添加昵称和头衔到背景"""
        if title_info and title_info.get("content", None):
            # 处理头衔
            t_c = title_info.get("color", None)
            title_color = self.color_map.get(
                int(1 if t_c is None else t_c), self.color_map[1]
            )
            title_content = title_info.get("content", "")

            # 创建头衔气泡
            title_bubble = self.create_title_bubble(title_content, title_color)
            title_position = self.title_position or (
                self.bubble_position[0],
                self.avatar_position[1] + self.title_bubble_offset,
            )
            background.paste(title_bubble, title_position, title_bubble)

            name_x = title_position[0] + title_bubble.width + self.title_bubble_name_gap
            nickname_position = self.nickname_position or (
                name_x,
                self._centered_nickname_y(
                    title_bubble.height, nickname, title_position[1]
                ),
            )
            self._draw_supersampled_nickname(
                background,
                nickname_position,
                nickname,
            )
        else:
            # 只绘制昵称
            self._draw_supersampled_nickname(
                background,
                self.nickname_position
                or (self.bubble_position[0], self.avatar_position[1]),
                nickname,
            )

    def _centered_nickname_y(self, title_bubble_height, nickname, title_y=None):
        """Center the nickname's logical glyph box against the title badge."""
        bbox = self.nickname_font.getbbox(nickname)
        glyph_height = bbox[3] - bbox[1]
        bubble_top = (
            self.avatar_position[1] + self.title_bubble_offset
            if title_y is None
            else title_y
        )
        glyph_top = bubble_top + (title_bubble_height - glyph_height) / 2
        return round(glyph_top - bbox[1])

    def _draw_supersampled_nickname(self, background, position, nickname):
        """在旧字体 box 内进行高分辨率绘制，不改变昵称尺寸和位置。"""
        scale = self.SCALE
        logical_bbox = self.nickname_font.getbbox(nickname)
        width = max(1, logical_bbox[2] - logical_bbox[0])
        height = max(1, logical_bbox[3] - logical_bbox[1])
        scaled_font = self.nickname_SCALE_font
        scaled_bbox = scaled_font.getbbox(nickname)
        overlay = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).text(
            (-scaled_bbox[0], -scaled_bbox[1]),
            nickname,
            fill=self.nickname_color,
            font=scaled_font,
            stroke_width=max(1, scale // 4),
            stroke_fill=self.nickname_color,
        )
        overlay = overlay.resize(
            (width, height),
            Image.Resampling.LANCZOS,
        )
        destination = (
            round(position[0] + logical_bbox[0]),
            round(position[1] + logical_bbox[1]),
        )
        background.alpha_composite(overlay, dest=destination)

    def resize_by_scale(self, image, scale_factor):
        w, h = image.size
        return image.resize(
            (int(w * scale_factor), int(h * scale_factor)), Image.Resampling.LANCZOS
        )

    def _safe_text_width(self, draw, text, font, fallback_char_width):
        """
        永不抛异常的文本宽度测量
        - 兼容 Pillow 新旧版本
        - font 为 None / glyph 缺失 / emoji 均可兜底
        """
        if not text:
            return 0

        try:
            if font is None:
                raise ValueError("font is None")

            # Pillow >= 8.x
            if hasattr(draw, "textbbox"):
                bbox = draw.textbbox((0, 0), text, font=font)
                w = bbox[2] - bbox[0]
            else:
                # Pillow < 8.x
                w = draw.textlength(text, font=font)

            # 非法结果兜底
            if not isinstance(w, (int, float)) or w <= 0:
                raise ValueError("invalid width")

            return int(w)

        except Exception:
            # 最终兜底：字符数 × 估算宽度
            try:
                return len(text) * int(fallback_char_width)
            except Exception:
                return 0

    def _create_single_gif_bubble_frame(self, frame, apply_scaling=True):
        """优化版本：快速创建单帧气泡，避免重复初始化"""
        # 调整图片大小
        if apply_scaling:
            # 在qq会被tx压缩图片,所以要先放大图片
            frame = self.resize_by_scale(frame, 2)

        # 调整图片以适应气泡
        padding = self.bubble_padding * (self.SCALE if apply_scaling else 1)
        max_width = self.max_width * (self.SCALE if apply_scaling else 1) - padding * 2
        orig_width, orig_height = frame.size

        if orig_width > max_width:
            ratio = max_width / orig_width
            new_width = int(orig_width * ratio)
            new_height = int(orig_height * ratio)
            frame = frame.resize((new_width, new_height), Image.Resampling.LANCZOS)

        width, height = frame.size

        # 创建圆角气泡
        if apply_scaling:
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            mask = self._create_rounded_mask(width, height)
            canvas.paste(frame, (0, 0), mask)

            # 缩放到正常尺寸
            if self.SCALE > 1:
                canvas = canvas.resize(
                    (width // self.SCALE, height // self.SCALE),
                    Image.Resampling.LANCZOS,
                )
            return canvas
        else:
            # 仅用于布局计算，不应用实际效果
            return Image.new("RGBA", (width, height), (0, 0, 0, 0))
