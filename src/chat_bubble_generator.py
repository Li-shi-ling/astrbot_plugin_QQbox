import os
import threading
import unicodedata
from functools import wraps
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageSequence

from astrbot.api import logger

from .font_manager import FontBundle


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

        # 文本测量上下文改为每线程一份，避免并发渲染竞争共享画布
        self._measure_state = threading.local()

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
        """获取测量用的临时绘图上下文（延迟初始化，每线程一份）。"""
        draw = getattr(self._measure_state, "draw", None)
        if draw is None:
            canvas = Image.new("RGBA", (10, 10))
            draw = ImageDraw.Draw(canvas)
            self._measure_state.canvas = canvas
            self._measure_state.draw = draw
        return draw

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

