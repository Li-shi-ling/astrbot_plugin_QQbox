from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from PIL import Image, ImageDraw, ImageFont
from astrbot.api.star import StarTools
from astrbot.api import AstrBotConfig
from astrbot.api import logger
from io import BytesIO
import traceback
import tempfile
import aiofiles
import asyncio
import httpx
import base64
import json
import re
import os


@register("QQbox", "Lishining", "我想要说的,群友都替我说了!", "1.0.0")
class QQbox(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.Config = config

        # 使用框架提供的标准数据目录
        self.data_dir = str(StarTools.get_data_dir())

        # 优先使用配置的路径，如果没有则使用标准数据目录
        avatar_path = self.Config.get("avatar_image_path")
        self.avatar_image_path = self._get_absolute_path(avatar_path) if avatar_path else os.path.join(self.data_dir,"avatars")

        # 字体路径使用绝对路径
        self.bubble_font_path = self._get_absolute_path(self.Config.get("bubble_font_path", ""))
        self.nickname_font_path = self._get_absolute_path(self.Config.get("nickname_font_path", ""))
        self.title_font_path = self._get_absolute_path(self.Config.get("title_font_path", ""))

        # 临时文件目录
        self.temp_path = os.path.join(self.data_dir, "temp")

        # 创建必要的目录
        os.makedirs(self.avatar_image_path, exist_ok=True)
        os.makedirs(self.temp_path, exist_ok=True)

        # QQ数据文件路径
        self.qq_data_file = os.path.join(self._get_absolute_path(avatar_path), "qq_data.json")

        # 初始化QQ数据
        self.qq_title_key = {}

        # 初始化气泡生成器
        self.qqbox = ChatBubbleGenerator(
            bubble_font_path=self.bubble_font_path,
            nickname_font_path=self.nickname_font_path,
            title_font_path=self.title_font_path,
            avatar_image_path=self.avatar_image_path,
        )

        # 初始化HTTP客户端（异步）
        self.http_client = None

        # 检查字体文件是否存在
        self._check_fonts()

    async def initialize(self):
        """异步初始化，创建HTTP客户端"""
        # 创建异步HTTP客户端
        self.http_client = httpx.AsyncClient(timeout=30.0)
        logger.info("QQbox 插件初始化完成")
        self.qq_title_key = await self._load_qq_data()

    async def terminate(self):
        """清理资源"""
        # 保存QQ数据
        await self._save_qq_data()

        # 关闭HTTP客户端
        if self.http_client:
            await self.http_client.aclose()
            logger.info("HTTP客户端已关闭")

    def _get_absolute_path(self, path):
        """将路径转换为绝对路径"""
        if not path:
            return ""
        return os.path.abspath(path)

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
                logger.warning(f"找不到{font_name}文件: {font_path}")

    async def _load_qq_data(self):
        """异步加载QQ数据"""
        try:
            if os.path.exists(self.qq_data_file):
                async with aiofiles.open(self.qq_data_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    if not content.strip():
                        return {}
                    return json.loads(content)
            return {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"加载QQ数据失败: {e}")
            return {}

    async def _save_qq_data(self):
        """保存QQ数据"""
        try:
            async with aiofiles.open(self.qq_data_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(self.qq_title_key, indent=4, ensure_ascii=False))
        except OSError as e:
            logger.error(f"保存QQ数据失败: {e}")

    def _validate_qq(self, qq):
        """验证QQ号是否合法（只包含数字）"""
        if not qq or not isinstance(qq, str):
            return False
        # 只允许数字，防止路径遍历攻击
        if not qq.isdigit():
            logger.warning(f"检测到非法QQ号格式: {qq}")
            return False
        return True

    @filter.command("QQbox_echo")
    async def QQbox_echo(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_echo")
        logger.info(f"进入QQbox_echo, params: {params}")
        if len(params) < 2:
            yield event.plain_result("请修正指令，应为 /echo [qq] [text]")
            return
        qq, text = params[0], " ".join(params[1:])
        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return
        tmp_path = None
        try:
            try:
                info = await get_qq_info(qq, self.avatar_image_path, self.http_client)
                if not info:
                    yield event.plain_result("获取QQ信息失败，请检查网络或稍后重试")
                    return
            except httpx.RequestError as e:
                logger.error(f"网络请求失败，QQ: {qq}, 错误: {e}")
                yield event.plain_result("网络请求失败，请检查网络连接")
                return
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP请求异常，状态码: {e.response.status_code}, QQ: {qq}")
                yield event.plain_result("服务暂时不可用，请稍后重试")
                return
            try:
                image = await asyncio.to_thread(
                    self.qqbox.create_chat_message,
                    qq=qq,
                    text=text,
                    image=None,
                    qq_title_key=self.qq_title_key,
                    user_info=info
                )
            except (MemoryError, OSError) as e:
                logger.error(f"图片生成失败，QQ: {qq}, 错误类型: {type(e).__name__}, 详情: {e}")
                yield event.plain_result("图片生成失败，可能是内存不足或系统资源限制")
                return
            except ImportError as e:
                logger.error(f"依赖库错误: {e}\n{traceback.format_exc()}")
                yield event.plain_result("系统组件异常，请联系管理员")
                return
            try:
                img_bytes = BytesIO()
                image.save(img_bytes, format='PNG')
                image_data = img_bytes.getvalue()
            except (IOError, OSError) as e:
                logger.error(f"图片保存失败，QQ: {qq}, 错误: {e}")
                yield event.plain_result("图片处理失败，请稍后重试")
                return
            try:
                fd, tmp_path = tempfile.mkstemp(suffix='.png', dir=self.temp_path)
                with os.fdopen(fd, 'wb') as f:
                    f.write(image_data)
            except (OSError, IOError) as e:
                logger.error(f"临时文件创建失败，QQ: {qq}, 错误: {e}")
                yield event.plain_result("文件操作失败，请检查磁盘空间")
                return
            try:
                yield event.make_result().file_image(tmp_path)
            except Exception as e:
                logger.error(f"消息发送失败，QQ: {qq}, 错误类型: {type(e).__name__}")
                yield event.plain_result("消息发送失败，请稍后重试")
                return
        except Exception as e:
            logger.error(
                f"未知错误，QQ: {qq}, 错误类型: {type(e).__name__}\n"
                f"完整堆栈: {traceback.format_exc()}\n"
                f"错误消息: {e}"
            )
            yield event.plain_result("系统内部错误，请联系管理员")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                    logger.debug(f"临时文件已清理: {tmp_path}")
                except OSError as e:
                    logger.warning(f"清理临时文件失败: {e}")

    @filter.command("QQbox_color")
    async def QQbox_color(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_color")
        logger.info(f"进入QQbox_color, params: {params}")

        if len(params) < 2:
            yield event.plain_result("请修正指令，应为 /QQbox_color [qq] [color]")
            return

        qq, color = params[0], params[1]

        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return

        await self._set_title_color(qq, color)
        yield event.plain_result(f"设置成功 qq:{qq}, color:{color}")

    @filter.command("QQbox_title")
    async def QQbox_title(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_title")
        logger.info(f"进入QQbox_title, params: {params}")

        if len(params) < 2:
            yield event.plain_result("请修正指令，应为 /QQbox_title [qq] [title]")
            return

        qq, title = params[0], " ".join(params[1:])

        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return

        await self._set_title_name(qq, title)
        yield event.plain_result(f"设置成功 qq:{qq}, title:{title}")

    @filter.command("QQbox_note")
    async def QQbox_note(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_note")
        logger.info(f"进入QQbox_note, params: {params}")

        if len(params) < 2:
            yield event.plain_result("请修正指令，应为 /QQbox_note [qq] [note]")
            return

        qq, note = params[0], " ".join(params[1:])

        if not self._validate_qq(qq):
            yield event.plain_result("QQ号格式错误，请使用纯数字")
            return

        await self._set_note(qq, note)
        yield event.plain_result(f"设置成功 qq:{qq}, note:{note}")

    @filter.command("QQbox_help")
    async def QQbox_help(self, event: AstrMessageEvent):
        help_text = """QQbox 插件使用说明

1. 生成聊天气泡
   命令：/QQbox_echo [QQ号] [消息内容]
   说明：生成指定QQ用户发送消息的气泡图片

2. 设置头衔颜色
   命令：/QQbox_color [QQ号] [颜色编号]
   说明：设置用户的头衔气泡背景颜色
   颜色编号：
   1 - 灰色（默认）
   2 - 紫色
   3 - 黄色
   4 - 绿色

3. 设置头衔内容
   命令：/QQbox_title [QQ号] [头衔文字]
   说明：设置用户显示的头衔内容

4. 设置备注名
   命令：/QQbox_note [QQ号] [备注名]
   说明：设置用户的显示备注名（会覆盖原昵称）

注意：所有QQ号都必须是纯数字格式"""
        yield event.plain_result(help_text)

    async def _set_note(self, qq, note):
        """设置备注名"""
        qq_str = str(qq)
        if qq_str not in self.qq_title_key:
            self.qq_title_key[qq_str] = {
                "color": None,
                "content": None,
                "notes": note
            }
        else:
            self.qq_title_key[qq_str]["notes"] = note

        await self._save_qq_data()

    async def _set_title_color(self, qq, color_id):
        """设置头衔颜色"""
        qq_str = str(qq)
        # 验证颜色ID
        match = re.search(r'[1-4]', color_id)
        color_clean = match.group() if match else "1"

        if qq_str not in self.qq_title_key:
            self.qq_title_key[qq_str] = {
                "color": color_clean,
                "content": "头衔",
                "notes": None
            }
        else:
            self.qq_title_key[qq_str]["color"] = color_clean

        await self._save_qq_data()

    async def _set_title_name(self, qq, title):
        """设置头衔名称"""
        qq_str = str(qq)
        if qq_str not in self.qq_title_key:
            self.qq_title_key[qq_str] = {
                "color": "1",
                "content": title,
                "notes": None
            }
        else:
            self.qq_title_key[qq_str]["content"] = title

        await self._save_qq_data()

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
            background_color="#F0F0F2"
    ):
        self.SCALE = 4  # supersampling 倍率

        # 加载字体（如果文件存在）
        self._load_fonts(
            bubble_font_path,
            nickname_font_path,
            title_font_path,
            bubble_font_size,
            nickname_font_size,
            title_font_size
        )

        # 配置参数
        self.title_padding_x = title_padding_x
        self.title_padding_y = title_padding_y
        self.bubble_font_size = bubble_font_size
        self.nickname_font_size = nickname_font_size
        self.title_font_size = title_font_size
        self.bubble_padding = bubble_padding
        self.bubble_bg_color = bubble_bg_color
        self.text_color = text_color
        self.corner_radius = corner_radius
        self.avatar_size = avatar_size
        self.margin = margin
        self.title_bubble_offset = title_bubble_offset
        self.title_padding_y_offset = title_padding_y_offset
        self.title_bubble_name_offset = title_bubble_name_offset
        self.max_width = max_width
        self.color_map = {
            1: (181, 182, 181, 220),  # #B5B6B5
            2: (214, 154, 255, 220),  # #D69AFF
            3: (255, 198, 41, 220),  # #FFC629
            4: (82, 215, 197, 220)  # #52D7C5
        }
        self.avatar_image_path = avatar_image_path
        self.bubble_position = bubble_position
        self.avatar_position = avatar_position
        self.background_color = background_color


    def _load_fonts(self, bubble_path, nickname_path, title_path, bubble_size, nickname_size, title_size):
        """加载字体文件，如果不存在则使用默认字体"""
        try:
            if bubble_path and os.path.exists(bubble_path):
                self.bubble_font = ImageFont.truetype(bubble_path, bubble_size * self.SCALE)
            else:
                self.bubble_font = ImageFont.load_default()
                logger.warning("使用默认气泡字体")
        except Exception as e:
            self.bubble_font = ImageFont.load_default()
            logger.warning(f"加载气泡字体失败，使用默认字体: {e}")

        try:
            if nickname_path and os.path.exists(nickname_path):
                self.nickname_font = ImageFont.truetype(nickname_path, nickname_size)
            else:
                self.nickname_font = ImageFont.load_default()
                logger.warning("使用默认昵称字体")
        except Exception as e:
            self.nickname_font = ImageFont.load_default()
            logger.warning(f"加载昵称字体失败，使用默认字体: {e}")

        try:
            if title_path and os.path.exists(title_path):
                self.title_SCALE_font = ImageFont.truetype(title_path, title_size * self.SCALE)
                self.title_font = ImageFont.truetype(title_path, title_size)
            else:
                self.title_SCALE_font = ImageFont.load_default()
                self.title_font = ImageFont.load_default()
                logger.warning("使用默认头衔字体")
        except Exception as e:
            self.title_SCALE_font = ImageFont.load_default()
            self.title_font = ImageFont.load_default()
            logger.warning(f"加载头衔字体失败，使用默认字体: {e}")

    # ------------------------------------------------------------------------------
    # 创建聊天气泡（高 DPI supersampling）
    # ------------------------------------------------------------------------------
    def create_chat_bubble(self, text):
        SCALE = self.SCALE
        font = self.bubble_font
        padding = self.bubble_padding * SCALE
        max_width = self.max_width * SCALE

        # 测量文本
        tmp = Image.new("RGBA", (10, 10))
        draw_tmp = ImageDraw.Draw(tmp)

        # 文本分行
        lines = []
        current = ""
        for ch in text:
            test = current + ch
            if ch == "\n":
                lines.append(current)
                current = ""
            else:
                try:
                    w = draw_tmp.textlength(test, font=font)
                except:
                    ch = " "
                    test = current + ch
                    w = draw_tmp.textlength(test, font=font)

                if w <= max_width - padding * 2:
                    current = test
                else:
                    lines.append(current)
                    current = ch
        if current:
            lines.append(current)

        # 计算文本高度
        bbox = font.getbbox("字")
        line_height = int(bbox[3] - bbox[1] + 4 * SCALE)
        text_height = line_height * len(lines)
        text_width = max(draw_tmp.textlength(line, font=font) for line in lines)

        # 创建气泡图像
        width = int(text_width + padding * 2)
        height = text_height + padding * (2 + len(lines))
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 绘制圆角矩形背景
        draw.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
            outline=(230, 230, 230, 255),
            width=2 * SCALE
        )

        # 绘制文本
        y = padding
        for line in lines:
            draw.text((padding, y), line, fill=self.text_color, font=font)
            y += line_height + padding

        # 缩放到正常尺寸
        img = img.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
        return img

    # ------------------------------------------------------------------------------
    # 创建聊天气泡（图片）
    # ------------------------------------------------------------------------------
    def create_chat_img_bubble(self, image):
        SCALE = self.SCALE
        max_width = self.max_width * SCALE

        # 加载图片
        if isinstance(image, str):
            img = Image.open(image)
        else:
            img = image

        # 缩放图片
        img = resize_by_scale(img, SCALE * 0.8)
        orig_width, orig_height = img.size

        # 调整大小以适应最大宽度
        if orig_width > max_width:
            width_ratio = max_width / orig_width
            new_width = int(orig_width * width_ratio)
            new_height = int(orig_height * width_ratio)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            new_width, new_height = orig_width, orig_height

        # 创建圆角遮罩
        canvas = Image.new("RGBA", (new_width, new_height), (0, 0, 0, 0))
        mask = Image.new("L", (new_width, new_height), 0)
        draw_mask = ImageDraw.Draw(mask)

        # 计算动态圆角半径
        min_side = min(new_width, new_height)
        radius_percentage = 0.05
        dynamic_radius = int(min_side * radius_percentage)
        max_radius = 50 * SCALE
        final_radius = min(dynamic_radius, max_radius)

        # 绘制圆角矩形遮罩
        draw_mask.rounded_rectangle(
            (0, 0, new_width, new_height),
            radius=final_radius,
            fill=255
        )

        # 应用遮罩
        canvas.paste(img, (0, 0), mask)

        # 缩放到正常尺寸
        if SCALE > 1:
            canvas = canvas.resize(
                (new_width // SCALE, new_height // SCALE),
                Image.Resampling.LANCZOS
            )

        return canvas

    # ------------------------------------------------------------------------------
    # 创建聊天气泡（图片 + 文字）
    # ------------------------------------------------------------------------------
    def create_chat_text_img_bubble(self, text, image):
        SCALE = self.SCALE
        font = self.bubble_font
        padding = self.bubble_padding * SCALE
        max_width = self.max_width * SCALE

        # 处理图片部分
        image = resize_by_scale(image, SCALE * 0.8)
        orig_width, orig_height = image.size

        if orig_width > max_width - 2 * padding:
            width_ratio = (max_width - 2 * padding) / orig_width
            new_width = int(orig_width * width_ratio)
            new_height = int(orig_height * width_ratio)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            new_width, new_height = orig_width, orig_height

        # 创建圆角图片
        canvas = Image.new("RGBA", (new_width, new_height), (0, 0, 0, 0))
        mask = Image.new("L", (new_width, new_height), 0)
        draw_mask = ImageDraw.Draw(mask)

        min_side = min(new_width, new_height)
        radius_percentage = 0.05
        dynamic_radius = int(min_side * radius_percentage)
        max_radius = 50 * SCALE
        final_radius = min(dynamic_radius, max_radius)

        draw_mask.rounded_rectangle(
            (0, 0, new_width, new_height),
            radius=final_radius,
            fill=255
        )
        canvas.paste(image, (0, 0), mask)

        # 处理文本部分
        tmp = Image.new("RGBA", (10, 10))
        draw_tmp = ImageDraw.Draw(tmp)

        lines = []
        current = ""
        for ch in text:
            test = current + ch
            if ch == "\n":
                lines.append(current)
                current = ""
            else:
                try:
                    w = draw_tmp.textlength(test, font=font)
                except:
                    ch = " "
                    test = current + ch
                    w = draw_tmp.textlength(test, font=font)

                if w <= max_width - padding * 2:
                    current = test
                else:
                    lines.append(current)
                    current = ch

        if current:
            lines.append(current)

        # 计算总尺寸
        bbox = font.getbbox("字")
        line_height = int(bbox[3] - bbox[1] + 4 * SCALE)
        text_height = line_height * len(lines)
        text_width = max(draw_tmp.textlength(line, font=font) for line in lines)

        width = int(text_width + padding * 2)
        height = text_height + padding * (2 + len(lines)) + canvas.height + padding

        # 创建最终气泡
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 绘制背景
        draw.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
            outline=(230, 230, 230, 255),
            width=2 * SCALE
        )

        # 绘制文本
        y = padding
        for line in lines:
            draw.text((padding, y), line, fill=self.text_color, font=font)
            y += line_height + padding

        # 粘贴图片
        img.paste(
            canvas,
            (
                padding,
                text_height + padding * (2 + len(lines) + 1),
                padding + new_width,
                text_height + padding * (2 + len(lines) + 1) + new_height
            ),
            canvas
        )

        # 缩放到正常尺寸
        img = img.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
        return img

    # ------------------------------------------------------------------------------
    # 创建头衔气泡
    # ------------------------------------------------------------------------------
    def create_title_bubble(self, text, bg_color):
        """创建头衔气泡"""
        SCALE = self.SCALE
        font = self.title_SCALE_font

        # 测量文本
        tmp = Image.new("RGBA", (10, 10))
        draw_tmp = ImageDraw.Draw(tmp)
        text_width = int(draw_tmp.textlength(text, font=font))

        # 计算字体高度
        bbox = font.getbbox(text)
        text_height = int(bbox[3] - bbox[1] + 4 * SCALE)

        # 添加内边距
        width = int(text_width + self.title_padding_x * 2)
        height = int(text_height + self.title_padding_y * 3)

        # 创建气泡
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 绘制背景
        draw.rounded_rectangle(
            (0, 0, width, height),
            radius=8 * SCALE,
            fill=bg_color
        )

        # 绘制文本
        draw.text(
            (self.title_padding_x, self.title_padding_y_offset),
            text,
            fill=(255, 255, 255, 255),
            font=font
        )

        # 缩放到正常尺寸
        img = img.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
        return img

    # ------------------------------------------------------------------------------
    # 创建完整聊天消息（异步版本）
    # ------------------------------------------------------------------------------
    def create_chat_message(
            self,
            qq,
            text,
            image,
            qq_title_key=None,
            user_info=None
    ):
        if user_info is None:
            raise ValueError("需要提供user_info参数，避免同步HTTP调用")

        nickname = user_info["name"]
        avatar_path = user_info["avatar_path"]

        # 创建气泡
        if text and (image is None):
            bubble = self.create_chat_bubble(text)
        elif image and not text:
            bubble = self.create_chat_img_bubble(image)
        else:
            bubble = self.create_chat_text_img_bubble(text, image)

        bubble_w, bubble_h = bubble.size

        # 测量文本宽度
        tmp = Image.new("RGBA", (10, 10))
        draw_tmp = ImageDraw.Draw(tmp)

        # 处理头衔信息
        qq_title = qq_title_key.get(qq, None) if qq_title_key else None
        is_title = qq_title is not None
        title_color = title_width = content = None

        if is_title:
            # 获取备注名（如果有）
            tmp_nickname = qq_title.get("notes", None)
            content = qq_title.get("content", "")
            title_color = qq_title.get("color", "1")

            if tmp_nickname is not None:
                nickname = tmp_nickname

            # 测量宽度
            nickname_width = int(draw_tmp.textlength(nickname, font=self.nickname_font)) + self.bubble_padding
            title_width = int(draw_tmp.textlength(content, font=self.title_font)) + self.bubble_padding

            # 计算背景宽度
            bg_w = max(
                self.bubble_position[0] + bubble_w + self.margin,
                self.avatar_position[0] + self.avatar_size[0] + self.margin,
                self.bubble_position[0] + nickname_width + title_width + self.title_bubble_name_offset
            )
        else:
            nickname_width = int(draw_tmp.textlength(nickname, font=self.nickname_font)) + self.bubble_padding
            bg_w = max(
                self.bubble_position[0] + bubble_w + self.margin,
                self.avatar_position[0] + self.avatar_size[0] + self.margin,
                self.bubble_position[0] + nickname_width
            )

        # 计算背景高度
        bg_h = max(
            self.bubble_position[1] + bubble_h + self.margin,
            self.avatar_position[1] + self.avatar_size[1] + self.margin
        )

        # 创建背景
        r = int(self.background_color[1:3], 16)
        g = int(self.background_color[3:5], 16)
        b = int(self.background_color[5:7], 16)
        background = Image.new("RGBA", (bg_w, bg_h), (r, g, b, 255))

        # 粘贴气泡
        background.paste(bubble, self.bubble_position, bubble)

        # 加载并粘贴头像
        try:
            avatar = Image.open(avatar_path).convert("RGBA")
            avatar = avatar.resize(self.avatar_size, Image.Resampling.LANCZOS)
            background.paste(avatar, self.avatar_position, avatar)
        except Exception as e:
            logger.error(f"加载头像失败: {e}")
            # 使用默认头像占位
            default_avatar = Image.new("RGBA", self.avatar_size, (200, 200, 200, 255))
            background.paste(default_avatar, self.avatar_position)

        # 绘制昵称和头衔
        draw = ImageDraw.Draw(background)

        if is_title:
            # 获取头衔颜色
            title_bg_color = self.color_map.get(int(title_color), self.color_map[1])

            # 创建并粘贴头衔气泡
            title_bubble = self.create_title_bubble(content, title_bg_color)
            background.paste(
                title_bubble,
                (self.bubble_position[0], self.avatar_position[1] + self.title_bubble_offset),
                title_bubble
            )

            # 绘制昵称
            draw.text(
                (self.bubble_position[0] + title_width + self.title_bubble_name_offset, self.avatar_position[1]),
                nickname,
                fill=self.text_color,
                font=self.nickname_font
            )
        else:
            # 只绘制昵称
            draw.text(
                (self.bubble_position[0], self.avatar_position[1]),
                nickname,
                fill=self.text_color,
                font=self.nickname_font
            )

        return background

# ------------------------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------------------------
def extract_help_parameters(s, directive):
    """提取指令参数"""
    escaped_directive = re.escape(directive)
    match = re.search(f'{escaped_directive}' + r'\s+(.*)', s)
    if match:
        params = re.split(r'\s+', match.group(1).strip())
        return params
    return []

async def get_qq_info(qq, avatar_cache_location=".", http_client=None):
    """异步获取QQ信息（缓存 + API）"""
    # 验证QQ号
    if not qq or not isinstance(qq, str) or not qq.isdigit():
        logger.warning(f"无效的QQ号格式: {qq}")
        return None

    # 确保缓存目录存在
    os.makedirs(avatar_cache_location, exist_ok=True)

    # 先检查缓存
    for filename in os.listdir(avatar_cache_location):
        if filename.startswith(f"{qq}-") and filename.endswith(".png"):
            nickname = filename[len(f"{qq}-"):-4]
            return {
                "qq": qq,
                "name": nickname,
                "avatar_path": os.path.join(avatar_cache_location, filename)
            }

    # 需要HTTP客户端
    if http_client is None:
        logger.error("HTTP客户端未初始化")
        return None

    # 异步请求API
    try:
        # 备用API列表
        apis = [
            f"https://api.mmp.cc/api/qqname?qq={qq}",
            f"https://api.uomg.com/api/qq.info?qq={qq}",
            # 可以添加更多备用API
        ]

        nickname = qq  # 默认值
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

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
            except Exception as e:
                logger.debug(f"API请求失败 {api_url}: {e}")
                continue

        # 下载头像
        save_path = os.path.join(avatar_cache_location, f"{qq}-{nickname}.png")
        success = await download_circular_avatar(avatar_url, save_path, http_client)

        if not success:
            logger.warning(f"下载头像失败: {qq}")
            # 创建默认头像
            create_default_avatar(qq, nickname, save_path)

        return {
            "qq": qq,
            "name": nickname,
            "avatar_path": save_path
        }

    except Exception as e:
        logger.error(f"获取QQ信息失败: {e}")
        return None

async def download_circular_avatar(url, save_path, http_client=None, size=None):
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
        result = create_circular_avatar(img, size)

        # 保存头像
        result.save(save_path)
        logger.debug(f"头像已保存: {save_path}")
        return True

    except httpx.RequestError as e:
        logger.error(f"下载头像请求失败: {e}")
    except Exception as e:
        logger.error(f"处理头像失败: {e}")

    return False

def create_circular_avatar(img, size=None):
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

def create_default_avatar(qq, nickname, save_path):
    """创建默认头像"""
    try:
        size = 200
        # 创建简单头像
        img = Image.new("RGB", (size, size), (100, 150, 200))
        draw = ImageDraw.Draw(img)

        # 绘制字母
        text = nickname[0].upper() if nickname else "Q"
        try:
            font = ImageFont.truetype("arial.ttf", 80)
        except:
            font = ImageFont.load_default()

        # 居中绘制文字
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        position = ((size - text_width) // 2, (size - text_height) // 2)

        draw.text(position, text, fill=(255, 255, 255), font=font)

        # 转换为圆形并保存
        circular = create_circular_avatar(img.convert("RGBA"))
        circular.save(save_path)
        return True
    except Exception as e:
        logger.error(f"创建默认头像失败: {e}")
        return False

def resize_by_scale(image, scale_factor):
    """按比例缩放图像"""
    w, h = image.size
    return image.resize((int(w * scale_factor), int(h * scale_factor)), Image.Resampling.LANCZOS)

def image_to_base64(image_obj, format="PNG") -> str:
    """将PIL Image对象转换为Base64字符串"""
    img_buffer = BytesIO()
    image_obj.save(img_buffer, format=format)
    img_bytes = img_buffer.getvalue()
    base64_str = base64.b64encode(img_bytes).decode("utf-8")
    return base64_str
