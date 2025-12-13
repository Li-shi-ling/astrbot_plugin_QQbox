from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from PIL import Image, ImageDraw, ImageFont
from astrbot.api import AstrBotConfig
from astrbot.api import logger
from io import BytesIO
import requests
import base64
import json
import os
import re

@register("QQbox", "Lishining", "我想要说的,群友都替我说了!", "1.0.0")
class QQbox(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.Config = config
        self.avatar_image_path = self.Config.get("avatar_image_path")
        self.qqbox = ChatBubbleGenerator(
            bubble_font_path="./data/plugins/astrbot_plugin_QQbox/resources/fonts/Microsoft-YaHei-Semilight.ttc",
            nickname_font_path="./data/plugins/astrbot_plugin_QQbox/resources/fonts/SourceHanSansSC-ExtraLight.otf",
            title_font_path="./data/plugins/astrbot_plugin_QQbox/resources/fonts/Microsoft-YaHei-Bold.ttc",
            avatar_image_path = self.avatar_image_path,
        )
        if not os.path.exists(os.path.join(self.avatar_image_path,"qq_data.json")):
            os.makedirs(os.path.dirname(self.avatar_image_path), exist_ok=True)
            self.qq_title_key = {}
        else:
            try:
                self.qq_title_key = read_json_file(os.path.join(self.avatar_image_path,"qq_data.json"))
            except:
                self.qq_title_key = {}
        self.temp_path = self.Config.get("temp_path")
        if not os.path.exists(self.temp_path):
            os.mkdir(self.temp_path)

    @filter.command("QQbox_echo")
    async def echo(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_echo")
        if len(params) < 2:
            yield event.plain_result("请修正指令,应为 /echo [qq] [text]")
            return
        qq, text = params[0], params[1]
        try:
            image = self.qqbox.create_chat_message(
                qq = qq,
                text = text,
                image = None,
                qq_title_key = self.qq_title_key
            )
        except Exception as e:
            yield event.plain_result(f"创建气泡错误:{e}")
            logger.warning(f"创建气泡错误:{e}")
            return
        image.save(os.path.join(self.temp_path,"temp.png"))
        yield event.make_result().file_image(os.path.join(self.temp_path,"temp.png"))
        try:
            os.remove(os.path.join(self.temp_path,"temp.png"))
            logger.info(f"文件 {os.path.join(self.temp_path,'temp.png')} 已成功删除")
        except FileNotFoundError:
            logger.warning(f"文件 {os.path.join(self.temp_path,'temp.png')} 不存在")
        except PermissionError:
            logger.warning(f"没有权限删除文件 {os.path.join(self.temp_path,'temp.png')}")
        except Exception as e:
            logger.error(f"删除文件时发生错误: {e}")
        return

    @filter.command("QQbox_color")
    async def Set_color(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_color")
        if len(params) < 2:
            yield event.plain_result("请修正指令,应为 /color [qq] [color]")
            return
        qq, color = params[0], params[1]
        self._set_title_color(qq, color)
        yield event.plain_result(f"设置成功qq:{qq},color:{color}")
        return

    @filter.command("QQbox_title")
    async def Set_title(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_title")
        if len(params) < 2:
            yield event.plain_result("请修正指令,应为 /title [qq] [title]")
            return
        qq, title = params[0], params[1]
        self._set_title_name(qq, title)
        yield event.plain_result(f"设置成功qq:{qq},title:{title}")
        return

    @filter.command("QQbox_note")
    async def Set_note(self, event: AstrMessageEvent):
        text = event.message_str
        params = extract_help_parameters(text, "QQbox_note")
        if len(params) < 2:
            yield event.plain_result("请修正指令,应为 /note [qq] [note]")
            return
        qq, note = params[0], params[1]
        self._set_note(qq, note)
        yield event.plain_result(f"设置成功qq:{qq},note:{note}")
        return

    @filter.command("QQbox_help")
    async def Set_note(self, event: AstrMessageEvent):
        output_text = """
        1.生成聊天气泡
        命令：/QQbox_echo [QQ号] [消息内容]
        说明：生成指定QQ用户发送消息的气泡图片
        示例：/QQbox_echo 123456 你好，今天天气不错！
        
        2.设置头衔颜色
        命令：/QQbox_color [QQ号] [颜色编号]
        说明：设置用户的头衔气泡背景颜色
        颜色编号：
          1 - 灰色（默认）
          2 - 紫色
          3 - 黄色
          4 - 绿色
        示例：/QQbox_color 123456 3
        
        3.设置头衔内容
        命令：/QQbox_title [QQ号] [头衔文字]
        说明：设置用户显示的头衔内容
        示例：/QQbox_title 123456 管理员
        
        4.设置备注名
        命令：/QQbox_note [QQ号] [备注名]
        说明：设置用户的显示备注名（会覆盖原昵称）
        示例：/QQbox_note 123456 张三
        """
        yield event.plain_result(output_text)
        return

    def _set_note(self, qq, note):
        if self.qq_title_key.get(str(qq),None) is None:
            self.qq_title_key[str(qq)] = {
                "color" : None,
                "content" : None,
                "notes": note
            }
        else:
            self.qq_title_key[str(qq)]["notes"] = note
        write_json_file(self.qq_title_key, os.path.join(self.avatar_image_path,"qq_data.json"))

    def _set_title_color(self, qq, color_id):
        # color = input("颜色:(1.灰色,2.紫色,3.黄色,4.绿色;请直接输入数字)")
        match = re.search(r'[1-4]', color_id)
        color_clean = match.group() if match else "1"
        if self.qq_title_key.get(str(qq),None) is None:
            self.qq_title_key[str(qq)] = {
                "color" : color_clean,
                "content" : "站位符",
                "notes" : None
            }
        else:
            self.qq_title_key[str(qq)]["color"] = color_clean
        write_json_file(self.qq_title_key, os.path.join(self.avatar_image_path,"qq_data.json"))

    def _set_title_name(self, qq, title):
        if self.qq_title_key.get(str(qq),None) is None:
            self.qq_title_key[str(qq)] = {
                "color" : "1",
                "content" : title,
                "notes" : None
            }
        else:
            self.qq_title_key[str(qq)]["content"] = title
        write_json_file(self.qq_title_key, os.path.join(self.avatar_image_path,"qq_data.json"))

    async def terminate(self):
        write_json_file(self.qq_title_key, os.path.join(self.avatar_image_path, "qq_data.json"))

# ------------------------------------------------------------------------------
# 高 DPI 超清聊天气泡生成器
# ------------------------------------------------------------------------------
class ChatBubbleGenerator:
    def __init__(
        self,
        bubble_font_path="./resources/fonts/Microsoft-YaHei-Semilight.ttc",
        nickname_font_path="./resources/fonts/SourceHanSansSC-ExtraLight.otf",
        title_font_path="./resources/fonts/Microsoft-YaHei-Bold.ttc",
        avatar_image_path = ".",
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
        max_width = 640
    ):
        self.SCALE = 4  # supersampling 倍率

        # 气泡字体
        self.bubble_font = ImageFont.truetype(bubble_font_path, bubble_font_size * self.SCALE)  if os.path.exists(bubble_font_path) else ImageFont.load_default()

        # 昵称字体
        self.nickname_font = ImageFont.truetype(nickname_font_path, nickname_font_size)  if os.path.exists(nickname_font_path) else ImageFont.load_default()

        # 头衔字体
        self.title_SCALE_font = ImageFont.truetype(title_font_path, title_font_size * self.SCALE)  if os.path.exists(nickname_font_path) else ImageFont.load_default()
        self.title_font = ImageFont.truetype(title_font_path, title_font_size) if os.path.exists(nickname_font_path) else ImageFont.load_default()

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


    # ------------------------------------------------------------------------------
    # 创建聊天气泡（高 DPI supersampling）
    # ------------------------------------------------------------------------------
    def create_chat_bubble(self, text):
        SCALE = self.SCALE
        font = self.bubble_font
        padding = self.bubble_padding * SCALE
        max_width = self.max_width * SCALE
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
        # 保留原 bbox 行高算法
        bbox = font.getbbox("字")
        line_height = int(bbox[3] - bbox[1] + 4 * SCALE)
        text_height = line_height * len(lines)
        text_width = max(draw_tmp.textlength(line, font=font) for line in lines)
        width = int(text_width + padding * 2)
        height = text_height + padding * (2 + len(lines))
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
            outline=(230, 230, 230, 255),
            width=2 * SCALE
        )

        y = padding
        for line in lines:
            draw.text((padding, y), line, fill=self.text_color, font=font)
            y += line_height + padding

        # 缩回正常尺寸实现高清
        img = img.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
        return img

    # ------------------------------------------------------------------------------
    # 创建聊天气泡（图片）
    # ------------------------------------------------------------------------------
    def create_chat_img_bubble(self, image):
        SCALE = self.SCALE
        max_width = self.max_width * SCALE
        if isinstance(image, str):
            img = Image.open(image)
        else:
            img = image
        img = resize_by_scale(img, SCALE * 0.8)
        orig_width, orig_height = img.size
        if orig_width > max_width:
            width_ratio = max_width / orig_width
            new_width = int(orig_width * width_ratio)
            new_height = int(orig_height * width_ratio)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            new_width, new_height = orig_width, orig_height
        canvas_width = new_width
        canvas_height = new_height
        canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
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
        canvas.paste(img, (-10, 0), mask)

        # 缩回正常尺寸实现高清
        if SCALE > 1:
            canvas = canvas.resize(
                (canvas_width // SCALE, canvas_height // SCALE),
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

        # 按比例缩放图片
        image = resize_by_scale(image, SCALE * 0.8)
        orig_width, orig_height = image.size
        if orig_width > max_width - 2 * padding:
            width_ratio = (max_width - 2 * padding) / orig_width
            new_width = int(orig_width * width_ratio)
            new_height = int(orig_height * width_ratio)
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            new_width, new_height = orig_width, orig_height
        canvas_width = new_width
        canvas_height = new_height
        canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
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
        canvas.paste(image, (-10, 0), mask)



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
        # 保留原 bbox 行高算法
        bbox = font.getbbox("字")
        line_height = int(bbox[3] - bbox[1] + 4 * SCALE)
        text_height = line_height * len(lines)
        text_width = max(draw_tmp.textlength(line, font=font) for line in lines)
        width = int(text_width + padding * 2)
        height = text_height + padding * (2 + len(lines)) + canvas.height + padding
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (0, 0, width, height),
            radius=self.corner_radius * SCALE,
            fill=self.bubble_bg_color,
            outline=(230, 230, 230, 255),
            width=2 * SCALE
        )

        y = padding
        for line in lines:
            draw.text((padding, y), line, fill=self.text_color, font=font)
            y += line_height + padding
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

        # 缩回正常尺寸实现高清
        img = img.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
        return img


    # ------------------------------------------------------------------------------
    # 添加创建头衔气泡的方法
    # ------------------------------------------------------------------------------
    def create_title_bubble(self, text, bg_color):
        """创建头衔气泡（与昵称气泡样式相同）"""
        SCALE = self.SCALE
        font = self.title_SCALE_font

        # 测量文本
        tmp = Image.new("RGBA", (10, 10))
        draw_tmp = ImageDraw.Draw(tmp)
        text_width = int(draw_tmp.textlength(text, font=font))

        # 获取字体高度
        bbox = font.getbbox(text)
        text_height = int(bbox[3] - bbox[1] + 4 * SCALE)

        # 添加内边距
        width = int(text_width + self.title_padding_x * 2)
        height = int(text_height + self.title_padding_y * 3)

        # 创建头衔气泡
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 绘制圆角矩形背景
        draw.rounded_rectangle(
            (0, 0, width, height),
            radius=8 * SCALE,
            fill=bg_color
        )

        # 绘制头衔文字（白色文字）
        draw.text(
            (self.title_padding_x, self.title_padding_y_offset),
            text,
            fill=(255, 255, 255, 255),
            font=font
        )

        # 缩回正常尺寸
        img = img.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS)
        return img

    # ------------------------------------------------------------------------------
    # 创建完整聊天消息（头像 + 气泡 + 昵称）
    # ------------------------------------------------------------------------------
    def create_chat_message(
        self,
        qq,
        text,
        image,
        qq_title_key = None,
        bubble_position=(120, 60),
        avatar_position=(23, 10),
        background_color="#F0F0F2"
    ):
        info = get_qq_info(qq ,self.avatar_image_path)
        assert info is not None, f"无法获取 QQ: {qq} 的信息"

        nickname = info["name"]
        avatar_path = info["avatar_path"]

        # 气泡
        if text and (image is None):
            bubble = self.create_chat_bubble(text)
        else:
            bubble = self.create_chat_img_bubble(image)
        bubble_w, bubble_h = bubble.size

        # 昵称宽度（正常尺寸）
        tmp = Image.new("RGBA", (10, 10))
        draw_tmp = ImageDraw.Draw(tmp)

        # 头衔
        qq_title = qq_title_key.get(qq, None)
        is_title = not qq_title is None
        title_color, title_width, content = None, None, None

        if is_title:
            tmp_nickname = qq_title.get("notes", None)
            content = qq_title.get("content", "")
            title_color = qq_title.get("color", "1")
            if not tmp_nickname is None:
                nickname = tmp_nickname
            nickname_width = int(draw_tmp.textlength(nickname, font=self.nickname_font)) + self.bubble_padding
            title_width = int(draw_tmp.textlength(content, font=self.title_font)) + self.bubble_padding
            bg_w = max(
                bubble_position[0] + bubble_w + self.margin,
                avatar_position[0] + self.avatar_size[0] + self.margin,
                bubble_position[0] + nickname_width + title_width + self.title_bubble_name_offset
            )
        else:
            nickname_width = int(draw_tmp.textlength(nickname, font=self.nickname_font)) + self.bubble_padding
            # 背景尺寸
            bg_w = max(
                bubble_position[0] + bubble_w + self.margin,
                avatar_position[0] + self.avatar_size[0] + self.margin,
                bubble_position[0] + nickname_width
            )

        # 背景尺寸
        bg_h = max(
            bubble_position[1] + bubble_h + self.margin,
            avatar_position[1] + self.avatar_size[1] + self.margin
        )

        # 背景
        r = int(background_color[1:3], 16)
        g = int(background_color[3:5], 16)
        b = int(background_color[5:7], 16)
        background = Image.new("RGBA", (bg_w, bg_h), (r, g, b, 255))

        # 贴气泡
        background.paste(bubble, bubble_position, bubble)

        # 贴头像
        avatar = Image.open(avatar_path).convert("RGBA")
        avatar = avatar.resize(self.avatar_size, Image.Resampling.LANCZOS)
        background.paste(avatar, avatar_position, avatar)

        # 昵称
        if is_title:

            title_bg_color = self.color_map.get(int(title_color), self.color_map[1])
            title_bubble = self.create_title_bubble(content, title_bg_color)
            background.paste(title_bubble, (bubble_position[0], avatar_position[1] + self.title_bubble_offset),
                             title_bubble)
            draw = ImageDraw.Draw(background)
            draw.text(
                (bubble_position[0] + title_width + self.title_bubble_name_offset, avatar_position[1]),
                nickname,
                fill=self.text_color,
                font=self.nickname_font
            )
        else:
            # 昵称
            draw = ImageDraw.Draw(background)
            draw.text(
                (bubble_position[0], avatar_position[1]),
                nickname,
                fill=self.text_color,
                font=self.nickname_font
            )
        return background

# ------------------------------------------------------------------------------
# 读取json文件
# ------------------------------------------------------------------------------
def read_json_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    return data

# ------------------------------------------------------------------------------
# 写入json文件
# ------------------------------------------------------------------------------
def write_json_file(data, file_path, indent=4, ensure_ascii=False):
    with open(file_path, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=indent, ensure_ascii=ensure_ascii)

# ------------------------------------------------------------------------------
# 获取指令后面的参数
# ------------------------------------------------------------------------------
def extract_help_parameters(s, directives):
    escaped_directives = re.escape(directives)
    match = re.search(f'{escaped_directives}' + r'\s+(.*)', s)
    if match:
        params = re.split(r'\s+', match.group(1).strip())
        return params
    return []

# ------------------------------------------------------------------------------
# 获取 QQ 信息（缓存 + API）
# ------------------------------------------------------------------------------
def get_qq_info(qq ,avatar_cache_location="."):
    avatar_cache = avatar_cache_location
    if not os.path.exists(avatar_cache):
        os.makedirs(avatar_cache)
    # 先查缓存
    for filename in os.listdir(avatar_cache):
        if filename.startswith(f"{qq}-") and filename.endswith(".png"):
            nickname = filename[len(f"{qq}-"):-4]
            return {
                "qq": qq,
                "name": nickname,
                "avatar_path": os.path.join(avatar_cache, filename)
            }
    # 请求 API
    # url = f"https://uapis.cn/api/v1/social/qq/userinfo?qq={qq}"
    url = f"http://api.mmp.cc/api/qqname?qq={qq}"
    res = requests.get(url)
    if res.status_code != 200:
        nickname = qq
    else:
        data = res.json()
        try:
            nickname = data["data"]["name"]
        except:
            nickname = qq
    # avatar_url = data.get("avatar_url")
    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    # avatar_url = f"http://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640&img_type=png"
    save_path = os.path.join(avatar_cache, f"{qq}-{nickname}.png")
    download_circular_avatar(avatar_url, save_path)
    return {
        "qq": qq,
        "name": nickname,
        "avatar_path": save_path
    }

# ------------------------------------------------------------------------------
# 剪为圆形 QQ 头像
# ------------------------------------------------------------------------------
def create_circular_avatar(img,size=None):
    # 中心裁剪正方形
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if size is None:
        size = min(w,h)
    # 调整大小
    img = img.resize((size, size), Image.Resampling.LANCZOS)

    # 创建圆形遮罩
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)

    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result

# ------------------------------------------------------------------------------
# 下载头像并裁剪为圆形
# ------------------------------------------------------------------------------
def download_circular_avatar(url, save_path="avatar.png", size=None):
    try:
        r = requests.get(url)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGBA")
        result = create_circular_avatar(img)
        result.save(save_path)
        return save_path
    except:
        return None

# ------------------------------------------------------------------------------
# 兼容性函数：按比例缩放图像
# ------------------------------------------------------------------------------
def resize_by_scale(image, scale_factor):
    w, h = image.size
    return image.resize((int(w * scale_factor), int(h * scale_factor)), Image.Resampling.LANCZOS)

# ------------------------------------------------------------------------------
# 将 PIL Image 对象转换为 Base64 字符串
# ------------------------------------------------------------------------------
def image_to_base64(image_obj, format="PNG") -> str:
    img_buffer = BytesIO()
    image_obj.save(img_buffer, format=format)
    img_bytes = img_buffer.getvalue()
    base64_str = base64.b64encode(img_bytes).decode("utf-8")
    return base64_str
