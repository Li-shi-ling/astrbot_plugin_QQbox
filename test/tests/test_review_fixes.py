from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import types
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from data.plugins.astrbot_plugin_QQbox.src.chat_bubble_generator import (
    ChatBubbleGenerator,
)
from data.plugins.astrbot_plugin_QQbox.src.layout import (
    DEFAULT_LAYOUT,
    LayoutValidationError,
    normalize_layout,
)
from data.plugins.astrbot_plugin_QQbox.src.font_manager import (
    ASTRBOT_GITHUB_MIRRORS,
    FontConfig,
    FontManager,
    FontState,
    FontStatus,
    FontVerifyError,
    format_font_generation_unavailable,
    format_font_status,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_layout_rejects_padding_that_leaves_no_image_width() -> None:
    layout = normalize_layout(DEFAULT_LAYOUT)
    layout["bubble"]["padding"] = 60
    layout["bubble"]["max_width"] = 120

    with pytest.raises(LayoutValidationError, match="padding"):
        normalize_layout(layout)


@pytest.mark.parametrize("role", ("bubble", "nickname", "title"))
def test_legacy_current_font_ids_migrate_to_role_default(role: str) -> None:
    layout = normalize_layout(DEFAULT_LAYOUT)
    layout[role]["font"] = f"current-{role}"

    assert normalize_layout(layout)[role]["font"] == ""


def test_current_font_id_cannot_cross_roles() -> None:
    layout = normalize_layout(DEFAULT_LAYOUT)
    layout["nickname"]["font"] = "current-title"

    with pytest.raises(LayoutValidationError, match="nickname.font"):
        normalize_layout(layout)


def test_frontend_guards_partial_number_input_and_uses_named_limit() -> None:
    app = (PLUGIN_ROOT / "pages/bubble-studio/app.js").read_text(encoding="utf-8")
    page = (PLUGIN_ROOT / "pages/bubble-studio/index.html").read_text(encoding="utf-8")

    assert "Number.isFinite" in app
    assert "input.value === \"\"" in app
    assert "MAX_MESSAGE_TEXT_LENGTH" in app
    assert 'maxlength="500"' not in page


def test_font_api_does_not_expose_runtime_current_ids(monkeypatch) -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    web = types.ModuleType("astrbot.api.web")
    api.logger = SimpleNamespace(error=lambda *_args, **_kwargs: None)
    web.request = SimpleNamespace(query={})
    web.error_response = lambda message, **_kwargs: {"error": message}
    web.json_response = lambda data, **_kwargs: data
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)

    module = importlib.import_module(
        "data.plugins.astrbot_plugin_QQbox.src.web_pages"
    )
    owner = SimpleNamespace(
        available_font_files=lambda: {
            "current-bubble": Path("runtime-bubble.otf"),
            "current-title": Path("runtime-title.otf"),
            "2.005R-cn/SourceHanSansCN-Normal.otf": Path("normal.otf"),
        }
    )

    result = asyncio.run(module.QQBoxWebController(owner).list_fonts())

    assert result == {
        "fonts": [
            {
                "id": "2.005R-cn/SourceHanSansCN-Normal.otf",
                "label": "normal.otf",
            }
        ]
    }


def test_download_switches_to_astrbot_mirror_after_direct_failure(tmp_path) -> None:
    requested_urls: list[str] = []

    async def downloader(url: str, path: Path, progress) -> None:
        requested_urls.append(url)
        if len(requested_urls) == 1:
            path.write_bytes(b"proxy error page")
        else:
            path.write_bytes(b"verified by test stub")
        progress({"downloaded": 21, "total": 21})

    manager = FontManager(
        tmp_path,
        PLUGIN_ROOT / "resources" / "font_manifest.json",
        FontConfig(),
        downloader=downloader,
    )

    def verify(path: Path) -> None:
        if path.read_bytes() != b"verified by test stub":
            raise FontVerifyError("invalid archive")

    manager._verify_archive = verify
    part_path = tmp_path / "font.zip.part"

    asyncio.run(manager._download_with_retries(part_path))

    assert requested_urls == [
        manager.manifest.url,
        f"{ASTRBOT_GITHUB_MIRRORS[0]}/{manager.manifest.url}",
    ]
    assert part_path.read_bytes() == b"verified by test stub"


def test_generation_message_reports_live_font_download_progress(tmp_path) -> None:
    status = FontStatus(
        state=FontState.DOWNLOADING,
        version="2.005R-cn",
        cache_path=tmp_path,
        error=None,
        downloaded=25,
        total=100,
    )

    message = format_font_generation_unavailable(status)

    assert "字体正在后台下载" in message
    assert "[███░░░░░░░░░]" in message
    assert "25 B / 100 B" in message
    assert "25.0%" in message
    assert "/qb font status" in message
    assert "\n\n" in message


def test_font_status_is_human_friendly_and_includes_progress_bar(tmp_path) -> None:
    status = FontStatus(
        state=FontState.DOWNLOADING,
        version="2.005R-cn",
        cache_path=tmp_path / "fonts",
        error=None,
        downloaded=5_191_721,
        total=50_700_226,
    )

    message = format_font_status(status)

    assert message.startswith("字体正在下载")
    assert "进度：[█░░░░░░░░░░░] 10.2%" in message
    assert "已下载：4.95 MB / 48.35 MB" in message
    assert "字体版本：2.005R-cn" in message
    assert "下载完成后会自动加载，无需重启插件" in message
    assert "\n\n" in message


def test_failed_font_status_explains_the_next_action(tmp_path) -> None:
    status = FontStatus(
        state=FontState.FAILED_DOWNLOAD,
        version="2.005R-cn",
        cache_path=tmp_path / "fonts",
        error="所有下载地址均不可用",
        downloaded=0,
        total=0,
    )

    message = format_font_status(status)

    assert message.startswith("字体下载失败")
    assert "原因：所有下载地址均不可用" in message
    assert "/qb font retry" in message
    assert "\n\n" in message


@pytest.mark.parametrize(
    "name", ("../outside.png", "folder/image.png", r"folder\image.png", "C:image.png")
)
def test_layout_rejects_background_paths(name: str) -> None:
    layout = normalize_layout(DEFAULT_LAYOUT)
    layout["bubble"]["background_image"] = name

    with pytest.raises(LayoutValidationError, match="纯文件名"):
        normalize_layout(layout)


def test_bubble_background_loader_stays_inside_persistent_directory(tmp_path) -> None:
    background_dir = tmp_path / "bubble_backgrounds"
    background_dir.mkdir()
    outside = tmp_path / "outside.png"
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(outside)
    valid = background_dir / "valid.png"
    Image.new("RGBA", (2, 2), (0, 255, 0, 255)).save(valid)

    generator = ChatBubbleGenerator(None, None, None, tmp_path)
    generator.bubble_background_dir = background_dir
    generator.bubble_background_image = "../outside.png"
    assert generator._load_bubble_background(8, 6) is None

    generator.bubble_background_image = "valid.png"
    rendered = generator._load_bubble_background(8, 6)
    assert rendered is not None
    try:
        assert rendered.size == (8, 6)
        assert rendered.getpixel((0, 0)) == (0, 255, 0, 255)
    finally:
        rendered.close()


def test_bubble_background_convert_failure_returns_none(
    monkeypatch, tmp_path
) -> None:
    import data.plugins.astrbot_plugin_QQbox.src.chat_bubble_generator as (
        bubble_module,
    )

    background = tmp_path / "broken.png"
    background.write_bytes(b"not inspected because Image.open is stubbed")

    class BrokenImage:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def convert(self, _mode):
            raise OSError("decode failed")

    monkeypatch.setattr(bubble_module.Image, "open", lambda _path: BrokenImage())
    generator = ChatBubbleGenerator(None, None, None, tmp_path)
    generator.bubble_background_dir = tmp_path
    generator.bubble_background_image = background.name

    assert generator._load_bubble_background(8, 6) is None


def _png_data_url(width: int = 2, height: int = 2) -> str:
    buffer = BytesIO()
    Image.new("RGBA", (width, height), (1, 2, 3, 255)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def test_image_upload_decoder_checks_size_and_dimensions(monkeypatch) -> None:
    module = importlib.import_module(
        "data.plugins.astrbot_plugin_QQbox.src.web_pages"
    )

    image = module._decode_uploaded_image(_png_data_url())
    try:
        assert image.mode == "RGBA"
        assert image.size == (2, 2)
    finally:
        image.close()

    monkeypatch.setattr(module, "MAX_IMAGE_PIXELS", 3)
    with pytest.raises(ValueError, match="尺寸过大"):
        module._decode_uploaded_image(_png_data_url())

    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(ValueError, match="8 MB"):
        module._decode_uploaded_image(_png_data_url())


def test_avatar_lookup_prefers_custom_and_supports_named_cache(tmp_path) -> None:
    module = importlib.import_module(
        "data.plugins.astrbot_plugin_QQbox.src.web_pages"
    )
    def cached_avatar_path(qq: str):
        custom = tmp_path / f"custom-{qq}.png"
        if custom.is_file():
            return custom
        return next(iter(sorted(tmp_path.glob(f"{qq}-*.png"))), None)

    owner = SimpleNamespace(
        avatar_image_path=tmp_path,
        _cached_avatar_path=cached_avatar_path,
    )
    controller = module.QQBoxWebController(owner)
    cached = tmp_path / "12345-Nickname.png"
    cached.write_bytes(b"cached")

    assert controller._avatar_path_for("12345") == (cached, False)

    custom = tmp_path / "custom-12345.png"
    custom.write_bytes(b"custom")
    assert controller._avatar_path_for("12345") == (custom, True)


def test_frontend_rejects_oversized_image_before_reading() -> None:
    for filename in ("app.js", "database.js"):
        source = (PLUGIN_ROOT / "pages/bubble-studio" / filename).read_text(
            encoding="utf-8"
        )
        assert "MAX_UPLOAD_BYTES" in source
        assert 'file.type.startsWith("image/")' in source
        assert "file.size > MAX_UPLOAD_BYTES" in source


def test_image_source_keeps_dictionary_adapter_compatibility() -> None:
    from data.plugins.astrbot_plugin_QQbox.main import QQbox

    assert QQbox._image_source(
        {"type": "image", "data": {"url": "https://example.test/image.png"}}
    ) == "https://example.test/image.png"
    assert QQbox._image_source(
        {"type": "image", "file": "C:/AstrBot/data/temp/image.png"}
    ) == "C:/AstrBot/data/temp/image.png"


def test_default_layout_keeps_global_background_as_dynamic_fallback() -> None:
    from data.plugins.astrbot_plugin_QQbox.main import QQbox

    plugin = QQbox.__new__(QQbox)
    plugin.qqbox = SimpleNamespace(
        bubble_position=(120, 60),
        avatar_position=(23, 10),
        background_color=(240, 240, 242, 255),
        margin=20,
        bubble_padding=20,
        corner_radius=27,
        max_width=640,
        bubble_bg_color=(255, 255, 255, 220),
        bubble_background_image="global.png",
        text_color=(0, 0, 0, 255),
        avatar_size=(89, 89),
        title_bubble_offset=5,
        title_padding_x=25,
        title_padding_y=15,
        title_color=(255, 255, 255, 255),
        nickname_color=(128, 128, 128, 255),
        _font_configs={
            "bubble": (None, 34),
            "title": (None, 19),
            "nickname": (None, 21),
        },
    )

    assert plugin.default_layout_config()["bubble"]["background_image"] == ""


def test_referenced_background_cannot_be_deleted(monkeypatch, tmp_path) -> None:
    module = importlib.import_module(
        "data.plugins.astrbot_plugin_QQbox.src.web_pages"
    )
    background = tmp_path / "used.png"
    background.write_bytes(b"background")
    layout = normalize_layout(DEFAULT_LAYOUT)
    layout["bubble"]["background_image"] = background.name

    class Presets:
        async def list_all(self):
            return [{"name": "正在使用的布局", "config": layout}]

    async def payload(default=None):
        return {"name": background.name}

    monkeypatch.setattr(module, "request", SimpleNamespace(json=payload))
    monkeypatch.setattr(
        module,
        "error_response",
        lambda message, **_kwargs: {"error": message},
    )
    owner = SimpleNamespace(
        bubble_background_dir=tmp_path,
        layout_preset_repo=Presets(),
        default_bubble_background="",
    )

    result = asyncio.run(module.QQBoxWebController(owner).delete_background())

    assert result["error"].startswith("该背景图正被布局预设使用")
    assert "正在使用的布局" in result["error"]
    assert background.is_file()
