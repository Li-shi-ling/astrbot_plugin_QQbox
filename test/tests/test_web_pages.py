from __future__ import annotations

import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_QQbox.src.layout import (
    DEFAULT_LAYOUT,
    LayoutValidationError,
    normalize_layout,
)
from data.plugins.astrbot_plugin_QQbox.src.web_pages import QQBoxWebController
from data.plugins.astrbot_plugin_QQbox.test.tests.test_main import run_async


def test_layout_normalization_fills_defaults_and_rejects_unsafe_values() -> None:
    layout = normalize_layout({"avatar": {"x": 41}, "bubble": {"font_size": 48}})

    assert layout["avatar"]["x"] == 41
    assert layout["avatar"]["width"] == DEFAULT_LAYOUT["avatar"]["width"]
    assert layout["bubble"]["font_size"] == 48
    assert layout["bubble"]["y"] == 60
    assert layout["canvas"]["auto_size"] is True
    assert layout["title"]["auto_position"] is True
    assert layout["nickname"]["auto_position"] is True
    assert layout["bubble"]["background_color"] == "#FFFFFFDC"

    legacy = normalize_layout(
        {
            "canvas": {"width": 760, "height": 280},
            "title": {"x": 120, "y": 15},
            "nickname": {"x": 330, "y": 25},
        }
    )
    assert legacy["canvas"]["auto_size"] is False
    assert legacy["title"]["auto_position"] is False
    assert legacy["nickname"]["auto_position"] is False

    with pytest.raises(LayoutValidationError):
        normalize_layout({"bubble": {"font": "../outside.ttf"}})
    with pytest.raises(LayoutValidationError):
        normalize_layout({"canvas": {"width": 10}})
    with pytest.raises(LayoutValidationError):
        normalize_layout({"nickname": {"color": "red"}})
    with pytest.raises(LayoutValidationError):
        normalize_layout({"avatar": {"unknown": 1}})


def test_layout_preset_repository_supports_crud_and_single_activation(qqbox) -> None:
    first = run_async(
        qqbox.layout_preset_repo.create("默认预设", normalize_layout(DEFAULT_LAYOUT))
    )
    second_layout = normalize_layout({"bubble": {"x": 180}, "avatar": {"x": 32}})
    second = run_async(qqbox.layout_preset_repo.create("右移预设", second_layout))

    assert [item["name"] for item in run_async(qqbox.layout_preset_repo.list_all())] == [
        "右移预设",
        "默认预设",
    ]
    active = run_async(qqbox.layout_preset_repo.activate(first["id"]))
    assert active["id"] == first["id"]
    active = run_async(qqbox.layout_preset_repo.activate(second["id"]))
    assert active["id"] == second["id"]
    assert sum(item["is_active"] for item in run_async(qqbox.layout_preset_repo.list_all())) == 1

    updated = run_async(
        qqbox.layout_preset_repo.update(second["id"], "移动后的预设", second_layout)
    )
    assert updated["name"] == "移动后的预设"
    assert updated["config"]["bubble"]["x"] == 180
    assert run_async(qqbox.layout_preset_repo.delete(first["id"])) is True
    assert run_async(qqbox.layout_preset_repo.get(first["id"])) is None
    assert run_async(qqbox.layout_preset_repo.activate(None)) is None
    assert run_async(qqbox.layout_preset_repo.get_active()) is None


def test_profile_repository_delete_supports_page_crud(qqbox) -> None:
    profile = {
        "nickname": "Cached",
        "notes": "数据库名称",
        "content": "数据库头衔",
        "color": 3,
    }
    run_async(qqbox.qq_profile_repo.upsert_profile("10001", profile))
    assert run_async(qqbox.qq_profile_repo.load_all())["10001"] == profile

    run_async(qqbox.qq_profile_repo.delete_profile("10001"))
    assert run_async(qqbox.qq_profile_repo.load_all()) == {}


def test_web_controller_registers_both_page_api_groups() -> None:
    routes = []

    class Context:
        def register_web_api(self, route, handler, methods, description):
            routes.append((route, handler, methods, description))

    QQBoxWebController(object()).register(Context())

    route_names = {route for route, *_ in routes}
    assert "/astrbot_plugin_QQbox/admin/profiles" in route_names
    assert "/astrbot_plugin_QQbox/admin/profiles/save" in route_names
    assert "/astrbot_plugin_QQbox/admin/profiles/delete" in route_names
    assert "/astrbot_plugin_QQbox/admin/layout/presets" in route_names
    assert "/astrbot_plugin_QQbox/admin/layout/preview" in route_names
    assert len(routes) == 11


def test_generator_accepts_independent_positions_and_fixed_canvas(
    generator, sample_avatar: Path
) -> None:
    generator.avatar_position = (14, 18)
    generator.title_position = (145, 24)
    generator.nickname_position = (360, 34)
    generator.bubble_position = (145, 105)
    generator.canvas_size = (840, 360)

    result = generator.create_chat_message(
        qq="10001",
        text="布局位置测试",
        image=None,
        qq_title_key={"10001": {"notes": "名称", "content": "头衔", "color": 4}},
        user_info={"name": "名称", "avatar_path": str(sample_avatar)},
    )

    from PIL import Image

    with Image.open(result) as image:
        assert image.size == (840, 360)


def test_plugin_renders_preview_with_real_freetype_font(qqbox, plugin_module) -> None:
    font_path = next(
        (
            path
            for path in (
                Path("C:/Windows/Fonts/arial.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            )
            if path.is_file()
        ),
        None,
    )
    if font_path is None:
        pytest.skip("no portable FreeType test font is installed")

    paths = plugin_module.FontPaths(
        bubble=font_path,
        nickname=font_path,
        title=font_path,
    )
    qqbox.qqbox = plugin_module.ChatBubbleGenerator(
        bubble_font_path=str(font_path),
        nickname_font_path=str(font_path),
        title_font_path=str(font_path),
        avatar_image_path=str(qqbox.avatar_image_path),
    )
    qqbox.qqbox.install_font_bundle(
        plugin_module.FontBundle(
            bubble=plugin_module.ImageFont.truetype(str(font_path), 136),
            nickname=plugin_module.ImageFont.truetype(str(font_path), 25),
            nickname_scaled=plugin_module.ImageFont.truetype(str(font_path), 100),
            title=plugin_module.ImageFont.truetype(str(font_path), 19),
            title_scaled=plugin_module.ImageFont.truetype(str(font_path), 76),
            paths=paths,
            version="test",
        )
    )

    payload = {
        "display_name": "Preview User",
        "title": "Example Title",
        "text": "Real font preview",
        "color": 4,
    }
    defaults = qqbox.default_layout_config()
    configured = qqbox._build_layout_generator(defaults)
    assert configured.bubble_position == (120, 60)
    assert configured.title_position is None
    assert configured.nickname_position is None
    assert configured.canvas_size is None

    base_result = qqbox.qqbox.create_chat_message(
        qq="10001",
        text=payload["text"],
        image=None,
        qq_title_key={
            "10001": {
                "notes": payload["display_name"],
                "content": payload["title"],
                "color": payload["color"],
            }
        },
        user_info={"name": payload["display_name"], "avatar_path": None},
    )
    result, resolved = qqbox.render_layout_preview_details(defaults, payload)

    assert result.getvalue().startswith(b"\x89PNG\r\n\x1a\n")
    assert result.getvalue() == base_result.getvalue()
    from PIL import Image

    with Image.open(result) as image:
        assert resolved["canvas"] == {"width": image.width, "height": image.height}
    assert resolved["title"]["x"] == 120
    assert resolved["title"]["y"] == 15


def test_plugin_pages_include_profile_crud_dragging_and_real_preview() -> None:
    plugin_root = Path(__file__).resolve().parents[2]
    page_entries = list((plugin_root / "pages").glob("*/index.html"))
    assert page_entries == [plugin_root / "pages/bubble-studio/index.html"]

    database_js = (plugin_root / "pages/bubble-studio/database.js").read_text(
        encoding="utf-8"
    )
    studio_html = (plugin_root / "pages/bubble-studio/index.html").read_text(
        encoding="utf-8"
    )
    studio_js = (plugin_root / "pages/bubble-studio/app.js").read_text(
        encoding="utf-8"
    )
    shell_js = (plugin_root / "pages/bubble-studio/shell.js").read_text(
        encoding="utf-8"
    )

    assert "用户资料管理" in studio_html
    assert 'data-view="bubble"' in studio_html
    assert 'data-view="database"' in studio_html
    assert 'data-view-panel="bubble"' in studio_html
    assert 'data-view-panel="database"' in studio_html
    assert "selectView" in shell_js
    assert "admin/profiles/save" in database_js
    assert "admin/profiles/delete" in database_js
    assert "预生成示范与布局预设" in studio_html
    assert "pointerdown" in studio_js
    assert "stage-image" in studio_html
    assert "requestPreview" in studio_js
    assert "admin/layout/presets/save" in studio_js
    assert "admin/layout/presets/reset" in studio_js
    assert "admin/layout/preview" in studio_js


def test_metadata_requires_plugin_pages_capable_astrbot() -> None:
    plugin_root = Path(__file__).resolve().parents[2]
    metadata = (plugin_root / "metadata.yaml").read_text(encoding="utf-8")
    assert "astrbot_version: \" >=4.26.3\"".replace(" ", "") in metadata.replace(
        " ", ""
    )


def test_default_layout_is_json_serializable() -> None:
    assert json.loads(json.dumps(normalize_layout(DEFAULT_LAYOUT))) == normalize_layout(
        DEFAULT_LAYOUT
    )
