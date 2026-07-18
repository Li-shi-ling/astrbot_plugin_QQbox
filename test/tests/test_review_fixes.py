from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

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
