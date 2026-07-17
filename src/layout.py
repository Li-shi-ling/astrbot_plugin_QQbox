from __future__ import annotations

import copy
import posixpath
import re
from typing import Any

COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")

DEFAULT_LAYOUT: dict[str, Any] = {
    "canvas": {
        "width": 760,
        "height": 280,
        "background_color": "#F0F0F2FF",
        "margin": 20,
    },
    "bubble": {
        "x": 120,
        "y": 82,
        "padding": 20,
        "corner_radius": 27,
        "max_width": 640,
        "background_color": "#FFFFFFDC",
        "text_color": "#000000FF",
        "font": "",
        "font_size": 34,
    },
    "avatar": {"x": 23, "y": 10, "width": 89, "height": 89},
    "title": {
        "x": 120,
        "y": 15,
        "padding_x": 25,
        "padding_y": 15,
        "font": "",
        "font_size": 19,
        "color": "#FFFFFFFF",
    },
    "nickname": {
        "x": 330,
        "y": 25,
        "font": "",
        "font_size": 25,
        "color": "#808080FF",
    },
}

NUMBER_RULES = {
    ("canvas", "width"): (320, 2000),
    ("canvas", "height"): (140, 2000),
    ("canvas", "margin"): (0, 200),
    ("bubble", "x"): (-500, 2000),
    ("bubble", "y"): (-500, 2000),
    ("bubble", "padding"): (0, 100),
    ("bubble", "corner_radius"): (0, 200),
    ("bubble", "max_width"): (120, 1600),
    ("bubble", "font_size"): (6, 200),
    ("avatar", "x"): (-500, 2000),
    ("avatar", "y"): (-500, 2000),
    ("avatar", "width"): (16, 512),
    ("avatar", "height"): (16, 512),
    ("title", "x"): (-500, 2000),
    ("title", "y"): (-500, 2000),
    ("title", "padding_x"): (0, 100),
    ("title", "padding_y"): (0, 100),
    ("title", "font_size"): (6, 200),
    ("nickname", "x"): (-500, 2000),
    ("nickname", "y"): (-500, 2000),
    ("nickname", "font_size"): (6, 200),
}

COLOR_FIELDS = {
    ("canvas", "background_color"),
    ("bubble", "background_color"),
    ("bubble", "text_color"),
    ("title", "color"),
    ("nickname", "color"),
}

FONT_FIELDS = {
    ("bubble", "font"),
    ("title", "font"),
    ("nickname", "font"),
}


class LayoutValidationError(ValueError):
    pass


def normalize_color(value: Any) -> str:
    if not isinstance(value, str) or not COLOR_RE.fullmatch(value):
        raise LayoutValidationError("颜色必须使用 #RRGGBB 或 #RRGGBBAA 格式")
    normalized = value.upper()
    return normalized if len(normalized) == 9 else f"{normalized}FF"


def color_tuple(value: str) -> tuple[int, int, int, int]:
    normalized = normalize_color(value)
    return tuple(int(normalized[index : index + 2], 16) for index in (1, 3, 5, 7))


def normalize_layout(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LayoutValidationError("布局必须是对象")

    normalized = copy.deepcopy(DEFAULT_LAYOUT)
    for section, defaults in DEFAULT_LAYOUT.items():
        incoming = payload.get(section, {})
        if not isinstance(incoming, dict):
            raise LayoutValidationError(f"{section} 必须是对象")
        unknown = set(incoming) - set(defaults)
        if unknown:
            raise LayoutValidationError(
                f"{section} 包含未知参数: {', '.join(sorted(unknown))}"
            )
        for field in defaults:
            key = (section, field)
            value = incoming.get(field, defaults[field])
            if key in NUMBER_RULES:
                if isinstance(value, bool):
                    raise LayoutValidationError(f"{section}.{field} 必须是整数")
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise LayoutValidationError(
                        f"{section}.{field} 必须是整数"
                    ) from exc
                minimum, maximum = NUMBER_RULES[key]
                if not minimum <= value <= maximum:
                    raise LayoutValidationError(
                        f"{section}.{field} 必须在 {minimum} 到 {maximum} 之间"
                    )
            elif key in COLOR_FIELDS:
                value = normalize_color(value)
            elif key in FONT_FIELDS:
                value = _normalize_font_id(value)
            normalized[section][field] = value
    return normalized


def _normalize_font_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 256:
        raise LayoutValidationError("字体标识无效")
    normalized = posixpath.normpath(value.replace("\\", "/"))
    if (
        normalized.startswith("../")
        or normalized in {".", ".."}
        or normalized.startswith("/")
        or ":" in normalized
    ):
        raise LayoutValidationError("字体标识不能越过字体持久化目录")
    return normalized
