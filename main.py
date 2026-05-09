import asyncio
import json
import os
from io import BytesIO
from pathlib import Path

import aiohttp
import httpx
from PIL import Image, ImageDraw, ImageFont, ImageSequence

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as BotImage
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, StarTools, register

from .src.db.repo import QQProfileRepo


@register("QQbox", "Lishining", "我想要说的,群友都替我说了!", "1.0.0")
class QQbox(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.Config = config

        # 获取圆角
        self.corner_radius = int(self.Config.get("corner_radius", 27))

        # 使用框架提供的标准数据目录
        self.data_dir = str(StarTools.get_data_dir())

        # 优先使用配置的路径，如果没有则使用标准数据目录
        avatar_path = self.Config.get("avatar_image_path", "")
        self.avatar_image_path = (
            os.path.join(self.data_dir, avatar_path)
            if avatar_path
            else os.path.join(self.data_dir, "avatars")
        )

        # 字体路径使用绝对路径
        self.bubble_font_path = self._get_absolute_path(
            self.Config.get("bubble_font_path", "")
        )
        self.nickname_font_path = self._get_absolute_path(
            self.Config.get("nickname_font_path", "")
        )
        self.title_font_path = self._get_absolute_path(
            self.Config.get("title_font_path", "")
        )

        # 创建必要的目录
        os.makedirs(self.avatar_image_path, exist_ok=True)

        # Legacy JSON path is kept only for one-time migration.
        self.qq_data_file = Path(self.avatar_image_path) / "qq_data.json"
        self.qq_db_file = Path(self.avatar_image_path) / "qqbox.db"
        self.qq_profile_repo = QQProfileRepo(self.qq_db_file)

        logger.debug(f"[qqbox] 使用:{self.qq_db_file}作为持久化数据存储位置")

        # 初始化QQ数据
        self.qq_title_key = {}

        # 初始化气泡生成器
        self.qqbox = ChatBubbleGenerator(
            bubble_font_path=self.bubble_font_path,
            nickname_font_path=self.nickname_font_path,
            title_font_path=self.title_font_path,
            avatar_image_path=self.avatar_image_path,
            corner_radius=self.corner_radius,
        )

        # 初始化HTTP客户端（异步）
        self.http_client = None

        # 检查字体文件是否存在
        self._check_fonts()

    # 插件函数
    @filter.command_group("qb")
    async def qb(self):
        pass

    @qb.command("echo")
    async def echo(self, event: AstrMessageEvent, qq: str):
        """通过对应qq的设置发送消息 /qb echo [qq] [text]"""
        if not self.qqbox.is_load_fonts:
            yield event.plain_result(
                "字体在加载中或字体没有被正确的加载,请尝试修改配置文件到正确的文字路径"
            )
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
        bot = getattr(event, "bot", None)
        info = await self.get_qq_info(qq, bot)
        img_bytes = await asyncio.to_thread(
            self.qqbox.create_chat_message,
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
            self.qqbox.create_chat_message_by_gif,
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
            yield event.plain_result(
                "字体在加载中或字体没有被正确的加载,请尝试修改配置文件到正确的文字路径"
            )
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
            self.qqbox.create_chat_message,
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

    @qb.command("help")
    async def get_help(self, event: AstrMessageEvent):
        """获取帮助 /qb help [qq]"""
        help_text = """QQbox 插件使用说明
1. 生成聊天气泡
   命令：/qb echo [QQ号] [消息内容]
   说明：生成指定QQ用户发送消息的气泡图片
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
注意：所有QQ号都必须是纯数字格式"""
        yield event.plain_result(help_text)

    # 生命周期管理
    # 启动插件时
    async def initialize(self):
        """异步初始化，创建HTTP客户端"""
        # 创建异步HTTP客户端
        self.http_client = httpx.AsyncClient(timeout=30.0)
        await self.qq_profile_repo.init_db()
        await self._migrate_legacy_qq_data()
        self.qq_title_key = await self._load_qq_data()
        self.qqbox.is_load_fonts = await self.qqbox.load_fonts()
        logger.info("QQbox 插件初始化完成")

    # 关闭插件时
    async def terminate(self):
        """清理资源"""
        # 保存QQ数据
        await self._save_qq_data()

        # 关闭HTTP客户端
        if self.http_client:
            await self.http_client.aclose()
            logger.info("HTTP客户端已关闭")
        await self.qq_profile_repo.close()

    # 工具方法
    # 保存QQ数据
    async def _save_qq_data(self):
        """保存QQ数据到数据库"""
        try:
            await self.qq_profile_repo.save_all(self.qq_title_key)
        except OSError as e:
            logger.error(f"保存QQ数据失败: {e}")

    # 获取qq数据
    async def _load_qq_data(self):
        """异步从数据库加载QQ数据"""
        try:
            return await self.qq_profile_repo.load_all()
        except OSError as e:
            logger.error(f"加载QQ数据失败: {e}")
            return {}

    async def _migrate_legacy_qq_data(self):
        """Import old JSON persistence into the SQLite database once."""
        legacy_data = self._load_legacy_qq_data()
        if not legacy_data:
            return

        await self.qq_profile_repo.save_all(legacy_data)
        try:
            self.qq_data_file.unlink()
        except OSError as e:
            logger.error(f"删除旧QQ数据失败: {e}")
        logger.info(f"[qqbox] 已从旧JSON迁移QQ数据: {self.qq_data_file}")

    def _load_legacy_qq_data(self):
        try:
            if not self.qq_data_file.exists():
                return {}
            content = self.qq_data_file.read_text(encoding="utf-8")
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
            logger.error(f"加载旧QQ数据失败: {e}")
            return {}

    # 检测字体是否存在
    def _check_fonts(self):
        """检查字体文件是否存在"""
        missing_fonts = []
        if self.bubble_font_path and not os.path.exists(self.bubble_font_path):
            missing_fonts.append(("气泡字体", self.bubble_font_path))
        if self.nickname_font_path and not os.path.exists(self.nickname_font_path):
            missing_fonts.append(("昵称字体", self.nickname_font_path))
        if self.title_font_path and not os.path.exists(self.title_font_path):
            missing_fonts.append(("头衔字体", self.title_font_path))

        if missing_fonts:
            for font_name, font_path in missing_fonts:
                logger.warning(f"[QQbox] 找不到{font_name}文件: {font_path}")

    # 获取绝路径
    def _get_absolute_path(self, path):
        """将路径转换为绝对路径"""
        if not path:
            return ""
        return os.path.abspath(path)

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
            if not nickname is None
            else qq_title.get("nickname", None),
            "color": color if not color is None else qq_title.get("color", None),
            "content": content
            if not content is None
            else qq_title.get("content", None),
            "notes": notes if not notes is None else qq_title.get("notes", None),
        }
        await self.qq_profile_repo.upsert_profile(qq, self.qq_title_key[qq])

    # 通过onebot获取nickname
    async def get_nickname_by_onebot(self, qq, bot=None):
        if bot is None:
            return None
        else:
            try:
                payloads = {"user_id": int(qq), "no_cache": True}
                qq_info = await bot.api.call_action("get_stranger_info", **payloads)
                return qq_info.get("nick", None)
            except:
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
                    return open(component.file, "rb").read()
            elif isinstance(component, Reply) and component.chain:
                for reply_component in component.chain:
                    if isinstance(reply_component, BotImage):
                        if reply_component.url:
                            return await self._download_image(reply_component.url)
                        elif reply_component.file:
                            return open(reply_component.file, "rb").read()
        return None

    # 通过url下载img
    async def _download_image(self, url: str) -> bytes | None:
        """下载图片"""
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
        title_padding_y_offset=8,
        title_bubble_offset=5,
        bubble_bg_color=(255, 255, 255, 220),
        text_color=(0, 0, 0, 255),
        corner_radius=27,
        avatar_size=(89, 89),
        margin=20,
        title_bubble_name_offset=-1,
        max_width=640,
        bubble_position=(120, 60),
        avatar_position=(23, 10),
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

        # 初始化字体
        # self.is_load_fonts = self._load_fonts()
        self.is_load_fonts = False

        # 布局参数
        self.bubble_padding = bubble_padding
        self.title_padding_x = title_padding_x
        self.title_padding_y = title_padding_y
        self.title_padding_y_offset = title_padding_y_offset
        self.title_bubble_offset = title_bubble_offset
        self.title_bubble_name_offset = title_bubble_name_offset
        self.margin = margin
        self.max_width = max_width
        self.corner_radius = corner_radius
        self.avatar_size = avatar_size
        self.bubble_position = bubble_position
        self.avatar_position = avatar_position

        # 样式参数
        self.bubble_bg_color = bubble_bg_color
        self.text_color = text_color
        self.avatar_image_path = avatar_image_path

        # 背景颜色处理
        if background_color.startswith("#"):
            self.background_color = tuple(
                int(background_color[i : i + 2], 16) for i in (1, 3, 5)
            ) + (255,)
        else:
            self.background_color = (240, 240, 242, 255)  # 默认颜色

    # ------------------------------------------------------------------------------
    # 字体管理
    # ------------------------------------------------------------------------------
    async def load_fonts(self):
        """异步加载字体"""
        try:
            # 气泡字体（高DPI）
            b_path, b_size = self._font_configs["bubble"]
            self.bubble_font = await self._async_safe_load_font(
                b_path, b_size * self.SCALE, "气泡"
            )
            # 昵称字体（正常DPI）
            n_path, n_size = self._font_configs["nickname"]
            self.nickname_font = await self._async_safe_load_font(
                n_path, n_size, "昵称"
            )
            # 头衔字体（双DPI版本）
            t_path, t_size = self._font_configs["title"]
            self.title_SCALE_font = await self._async_safe_load_font(
                t_path, t_size * self.SCALE, "头衔高DPI"
            )
            self.title_font = await self._async_safe_load_font(t_path, t_size, "头衔")
            return True
        except Exception as e:
            logger.error(f"字体加载失败: {e}")
            return False

    async def _async_safe_load_font(self, path, size, name):
        if path and os.path.exists(path):
            return await asyncio.to_thread(ImageFont.truetype, path, size)
        else:
            logger.warning(f"字体文件不存在: {path}")
            raise FileNotFoundError(f"字体文件不存在: {name} ({path})")

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
        """文本自动换行"""
        draw = self._get_temp_draw()
        padding = self.bubble_padding * self.SCALE
        max_width = self.max_width * self.SCALE - padding * 2

        lines = []

        # 首先按显式换行符分割成段落
        paragraphs = text.split("\n")

        for paragraph in paragraphs:
            if not paragraph:
                # 空段落（连续换行符）
                lines.append("")
                continue

            current_line = ""
            current_line_width = 0

            # 按字符处理段落
            for char in paragraph:
                # 测试添加字符后的宽度
                test_line = current_line + char

                try:
                    # 使用 textbbox 替代 textlength（更可靠）
                    if hasattr(draw, "textbbox"):
                        bbox = draw.textbbox((0, 0), test_line, font=font)
                        line_width = bbox[2] - bbox[0]
                    else:
                        # 旧版本 Pillow 兼容
                        line_width = draw.textlength(test_line, font=font)

                    # 检查是否需要换行
                    if line_width <= max_width:
                        current_line = test_line
                        current_line_width = line_width
                    else:
                        # 当前行已满，开始新行
                        if current_line:
                            lines.append(current_line)
                        current_line = char
                        # 计算新行的初始宽度
                        if hasattr(draw, "textbbox"):
                            bbox = draw.textbbox((0, 0), char, font=font)
                            current_line_width = bbox[2] - bbox[0]
                        else:
                            current_line_width = draw.textlength(char, font=font)

                except Exception as e:
                    # 如果测量失败，使用保守的字符宽度估计
                    logger.debug(f"测量文本宽度失败: {e}, 字符: {repr(char)}")

                    # 估计字符宽度：中文字符≈字体大小，英文字符≈字体大小/2
                    char_width_estimate = self._font_configs["bubble"][1] * self.SCALE
                    if ord(char) < 128:  # ASCII字符
                        char_width_estimate = char_width_estimate // 2

                    if current_line_width + char_width_estimate > max_width:
                        if current_line:
                            lines.append(current_line)
                        current_line = char
                        current_line_width = char_width_estimate
                    else:
                        current_line = test_line
                        current_line_width += char_width_estimate

            # 添加段落的最后一行
            if current_line:
                lines.append(current_line)

        return lines

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

    # ------------------------------------------------------------------------------
    # 气泡创建方法
    # ------------------------------------------------------------------------------
    def create_chat_bubble(self, text):
        """创建纯文本聊天气泡"""
        SCALE = self.SCALE
        font = self.bubble_font
        padding = self.bubble_padding * SCALE

        # 文本换行
        lines = self._wrap_text(text, font)
        if not lines:
            lines = [""]

        # 计算尺寸
        draw = self._get_temp_draw()
        bbox = font.getbbox("字")
        line_height = bbox[3] - bbox[1] + 4 * SCALE

        text_width = max(draw.textlength(line, font=font) for line in lines)
        text_height = line_height * len(lines)

        width = int(text_width + padding * 2)
        height = int(text_height + padding * (2 + len(lines)))

        # 创建画布
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_canvas = ImageDraw.Draw(canvas)

        # 绘制气泡背景
        draw_canvas.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
            outline=(230, 230, 230, 255),
            width=2 * SCALE,
        )

        # 绘制文本
        y = padding
        for line in lines:
            draw_canvas.text((padding, y), line, fill=self.text_color, font=font)
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
        lines = self._wrap_text(text, font) if text else []

        # 计算尺寸
        draw = self._get_temp_draw()
        bbox = font.getbbox("字")
        line_height = bbox[3] - bbox[1] + 4 * SCALE

        if lines:
            text_width = max(draw.textlength(line, font=font) for line in lines)
            text_height = line_height * len(lines)
        else:
            text_width = text_height = 0

        width = int(max(text_width, img_canvas.width) + padding * 2)
        height = int(
            text_height + padding * (2 + len(lines)) + img_canvas.height + padding
        )

        # 创建最终画布
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_canvas = ImageDraw.Draw(canvas)

        # 绘制气泡背景
        draw_canvas.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
            outline=(230, 230, 230, 255),
            width=2 * SCALE,
        )

        # 绘制文本
        if lines:
            y = padding
            for line in lines:
                draw_canvas.text((padding, y), line, fill=self.text_color, font=font)
                y += line_height + padding

        # 粘贴图片
        img_x = (width - img_canvas.width) // 2
        img_y = text_height + padding * (2 + len(lines) if lines else 1)
        canvas.paste(img_canvas, (img_x, img_y), img_canvas)

        # 缩放到正常尺寸
        return canvas.resize(
            (width // SCALE, height // SCALE), Image.Resampling.LANCZOS
        )

    def create_title_bubble(self, text, bg_color):
        """创建头衔气泡"""
        SCALE = self.SCALE
        font = self.title_SCALE_font

        # 测量文本
        draw = self._get_temp_draw()
        text_width = int(draw.textlength(text, font=font))

        # 计算字体高度
        bbox = font.getbbox(text)
        text_height = bbox[3] - bbox[1] + 4 * SCALE

        # 计算尺寸
        width = int(text_width + self.title_padding_x * 2)
        height = int(text_height + self.title_padding_y * 3)

        # 创建气泡
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_canvas = ImageDraw.Draw(canvas)

        # 绘制背景
        draw_canvas.rounded_rectangle(
            (0, 0, width, height), radius=8 * SCALE, fill=bg_color
        )

        # 绘制文本
        draw_canvas.text(
            (self.title_padding_x, self.title_padding_y_offset),
            text,
            fill=(255, 255, 255, 255),
            font=font,
        )

        # 缩放到正常尺寸
        return canvas.resize(
            (width // SCALE, height // SCALE), Image.Resampling.LANCZOS
        )

    # ------------------------------------------------------------------------------
    # 主要接口（保持签名不变）
    # ------------------------------------------------------------------------------
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
        background.paste(bubble, self.bubble_position, bubble)

        # 添加头像
        self._add_avatar(background, avatar_path)

        # 添加昵称和头衔
        self._add_name_and_title(background, nickname, title_info)

        # 返回字节流
        img_bytes = BytesIO()
        background.save(img_bytes, format="PNG", optimize=True)
        img_bytes.seek(0)
        return img_bytes

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
            background.paste(bubble, bubble_pos, bubble)

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

        # 如果有头衔，调整宽度
        if title_info and (not title_info.get("content", None) is None):
            title_width = (
                draw.textlength(title_info.get("content", ""), font=self.title_font)
                + self.bubble_padding
            )
            width_candidates.append(
                self.bubble_position[0]
                + nickname_width
                + title_width
                + self.title_bubble_name_offset
            )

        # 计算高度
        height_candidates = [
            self.bubble_position[1] + bubble_h + self.margin,
            self.avatar_position[1] + self.avatar_size[1] + self.margin,
        ]

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
                background.paste(avatar, self.avatar_position, avatar)
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
        draw = ImageDraw.Draw(background)

        if title_info and title_info.get("content", None):
            # 处理头衔
            t_c = title_info.get("color", None)
            title_color = self.color_map.get(
                int(1 if t_c is None else t_c), self.color_map[1]
            )
            title_content = title_info.get("content", "")

            # 创建头衔气泡
            title_bubble = self.create_title_bubble(title_content, title_color)
            background.paste(
                title_bubble,
                (
                    self.bubble_position[0],
                    self.avatar_position[1] + self.title_bubble_offset,
                ),
                title_bubble,
            )

            # 测量头衔宽度
            draw_temp = self._get_temp_draw()
            title_width = (
                draw_temp.textlength(title_content, font=self.title_font)
                + self.bubble_padding
            )

            # 绘制昵称
            name_x = (
                self.bubble_position[0] + title_width + self.title_bubble_name_offset
            )
            draw.text(
                (name_x, self.avatar_position[1]),
                nickname,
                fill=self.text_color,
                font=self.nickname_font,
            )
        else:
            # 只绘制昵称
            draw.text(
                (self.bubble_position[0], self.avatar_position[1]),
                nickname,
                fill=self.text_color,
                font=self.nickname_font,
            )

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
        SCALE = self.SCALE if apply_scaling else 1  # 布局计算时不需要缩放

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
