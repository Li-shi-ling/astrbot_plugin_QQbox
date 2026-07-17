from __future__ import annotations

import asyncio
import json
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageSequence

from data.plugins.astrbot_plugin_QQbox.test.conftest import FakeEvent
from data.plugins.astrbot_plugin_QQbox.src.font_manager import FontStatus


def run_async(coro):
    return asyncio.run(coro)


def make_png_bytes(color=(12, 34, 56, 255), size=(12, 12)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def make_gif_image() -> Image.Image:
    frames = [
        Image.new("RGBA", (16, 16), (255, 0, 0, 255)),
        Image.new("RGBA", (16, 16), (0, 255, 0, 255)),
    ]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[10, 80],
        loop=0,
    )
    buffer.seek(0)
    return Image.open(buffer)


class FakeJsonResponse:
    def __init__(self, status_code=200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeImageResponse:
    def __init__(self, payload: bytes) -> None:
        self.content = payload

    def raise_for_status(self) -> None:
        return None


class FakeAsyncClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    async def get(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize(
    ("qq", "expected"),
    [
        ("123456", True),
        ("00001", True),
        ("12a34", False),
        ("12 34", False),
        ("", False),
        (None, False),
        (123456, False),
    ],
)
def test_validate_qq(qqbox, qq, expected) -> None:
    assert qqbox._validate_qq(qq) is expected


def test_init_uses_plugin_data_dir_for_persistence(plugin_module, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", staticmethod(lambda: tmp_path))

    instance = plugin_module.QQbox(
        context=object(),
        config={
            "avatar_image_path": "/tmp/ignored",
            "bubble_font_path": "",
            "nickname_font_path": "",
            "title_font_path": "",
        },
    )

    assert instance.data_dir == tmp_path
    assert instance.avatar_image_path == tmp_path / "avatars"
    assert instance.qq_data_file == tmp_path / "qq_data.json"
    assert instance.qq_db_file == tmp_path / "db" / "qqbox.db"


def test_font_config_schema_defaults_to_persistent_downloads(plugin_module) -> None:
    schema_path = Path(plugin_module.__file__).resolve().parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["bubble_font_path"]["default"] == ""
    assert schema["nickname_font_path"]["default"] == ""
    assert schema["title_font_path"]["default"] == ""
    assert schema["font_auto_download"]["default"] is True
    assert schema["font_github_mirror"]["default"] == ""


def test_init_migrates_old_bundled_paths_and_keeps_persistent_font_root(
    plugin_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", staticmethod(lambda: tmp_path))
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "resources").mkdir(parents=True)
    source_manifest = Path(plugin_module.__file__).resolve().parent / "resources" / "font_manifest.json"
    (plugin_dir / "resources" / "font_manifest.json").write_bytes(source_manifest.read_bytes())
    monkeypatch.setattr(plugin_module, "__file__", str(plugin_dir / "main.py"))

    instance = plugin_module.QQbox(
        context=object(),
        config={
            "bubble_font_path": "/missing/astrbot_plugin_qqbox/resources/fonts/Microsoft-YaHei-Semilight.ttc",
            "nickname_font_path": "/missing/astrbot_plugin_qqbox/resources/fonts/SourceHanSansSC-ExtraLight.otf",
            "title_font_path": "/missing/astrbot_plugin_qqbox/resources/fonts/Microsoft-YaHei-Bold.ttc",
        },
    )

    assert instance.font_manager.font_root == (tmp_path / "fonts").resolve()
    assert not instance.font_manager.font_root.is_relative_to(plugin_dir)
    assert instance.bubble_font_path == ""
    assert instance.nickname_font_path == ""
    assert instance.title_font_path == ""


def test_legacy_font_config_is_saved_but_external_paths_are_preserved(
    plugin_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", staticmethod(lambda: tmp_path))

    class ConfigStub(dict):
        def __init__(self):
            super().__init__(
                bubble_font_path=(
                    "C:/astrbot/data/plugins/astrbot_plugin_QQbox/resources/fonts/"
                    "Microsoft-YaHei-Semilight.ttc"
                ),
                nickname_font_path="D:/user-fonts/custom.otf",
                title_font_path="",
            )
            self.saved = 0

        def save_config(self):
            self.saved += 1

    config = ConfigStub()
    instance = plugin_module.QQbox(context=object(), config=config)

    assert config["bubble_font_path"] == ""
    assert config["nickname_font_path"] == "D:/user-fonts/custom.otf"
    assert config.saved == 1
    assert instance.nickname_font_path == "D:/user-fonts/custom.otf"


def test_log_font_not_ready_paths_prints_runtime_paths_once(qqbox, plugin_module, monkeypatch) -> None:
    warnings = []

    class LoggerStub:
        def warning(self, message):
            warnings.append(message)

    monkeypatch.setattr(plugin_module, "logger", LoggerStub())

    qqbox._log_font_not_ready_paths()
    qqbox._log_font_not_ready_paths()

    assert warnings[0] == "[qqbox] 字体未加载，打印运行路径用于排查"
    assert sum("字体未加载" in message for message in warnings) == 1
    assert any("持久化数据目录" in message for message in warnings)
    assert any("字体持久化目录" in message for message in warnings)
    assert any("气泡自定义字体" in message for message in warnings)


def test_initialize_starts_font_preparation_without_awaiting_it(
    plugin_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", staticmethod(lambda: tmp_path))
    instance = plugin_module.QQbox(context=object(), config={})

    class FontManagerStub:
        def __init__(self):
            self.started = 0
            self.closed = 0
            self.font_root = tmp_path / "fonts"

        def start(self):
            self.started += 1
            return object()

        async def close(self):
            self.closed += 1

    stub = FontManagerStub()
    instance.font_manager = stub

    async def scenario():
        await instance.initialize()
        assert stub.started == 1
        await instance.terminate()
        assert stub.closed == 1

    run_async(scenario())


def test_font_status_and_retry_commands(qqbox, plugin_module) -> None:
    class FontManagerStub:
        def __init__(self):
            self.retries = 0
            self.needs_update = False

        def status(self):
            return FontStatus(
                state=plugin_module.FontState.DOWNLOADING,
                version="2.005R-cn",
                cache_path=Path("/persistent/fonts/2.005R-cn"),
                error=None,
                downloaded=10,
                total=20,
            )

        def retry(self):
            self.retries += 1

    class EventStub:
        @staticmethod
        def plain_result(value):
            return value

    qqbox.font_manager = FontManagerStub()

    async def collect(action):
        return [item async for item in qqbox.font(EventStub(), action)]

    status = run_async(collect("status"))
    retried = run_async(collect("retry"))

    assert "downloading" in status[0]
    assert "10/20" in status[0]
    assert "已启动" in retried[0]
    assert qqbox.font_manager.retries == 1


def test_echo_accepts_messages_longer_than_fifty_characters(
    qqbox, monkeypatch
) -> None:
    text = "这是一段用来验证长消息不再被五十字限制拦截的文本内容。" * 3

    class GeneratorStub:
        is_load_fonts = True

        @staticmethod
        def create_chat_message(**kwargs):
            assert kwargs["text"] == text
            return BytesIO(b"rendered-image")

    class EventStub:
        message_str = f"qb echo 10001 {text}"
        bot = None

        @staticmethod
        def plain_result(value):
            return ("plain", value)

        @staticmethod
        def chain_result(value):
            return ("chain", value)

    async def get_qq_info(_qq, _bot):
        return {"nickname": "tester"}

    qqbox.qqbox = GeneratorStub()
    monkeypatch.setattr(qqbox, "get_qq_info", get_qq_info)

    async def collect():
        return [item async for item in qqbox.echo(EventStub(), "10001")]

    result = run_async(collect())

    assert result[0][0] == "chain"
    assert result[0][1][0].payload == b"rendered-image"


def test_load_qq_data_reads_database_profiles(qqbox) -> None:
    assert run_async(qqbox._load_qq_data()) == {}

    qqbox.qq_title_key = {
        "10001": {
            "nickname": "Amiya",
            "color": 4,
            "content": "Leader",
            "notes": "Rabbit",
        }
    }
    run_async(qqbox._save_qq_data())

    assert run_async(qqbox._load_qq_data()) == {
        "10001": {
            "nickname": "Amiya",
            "color": 4,
            "content": "Leader",
            "notes": "Rabbit",
        }
    }


def test_save_qq_data_replaces_stale_database_profiles(qqbox) -> None:
    qqbox.qq_title_key = {
        "10001": {
            "nickname": "Amiya",
            "color": 4,
            "content": "Leader",
            "notes": "Rabbit",
        },
        "10002": {
            "nickname": "Doctor",
            "color": 1,
            "content": "Visitor",
            "notes": None,
        },
    }
    run_async(qqbox._save_qq_data())

    qqbox.qq_title_key = {
        "10002": {
            "nickname": "Doctor",
            "color": 3,
            "content": "Commander",
            "notes": "Doc",
        }
    }
    run_async(qqbox._save_qq_data())

    assert run_async(qqbox._load_qq_data()) == {
        "10002": {
            "nickname": "Doctor",
            "color": 3,
            "content": "Commander",
            "notes": "Doc",
        }
    }


def test_concurrent_profile_updates_are_persisted(qqbox) -> None:
    async def update_many() -> None:
        await asyncio.gather(
            *[
                qqbox.update_qq_title_key(str(10000 + index), nickname=f"user-{index}")
                for index in range(20)
            ]
        )

    run_async(update_many())

    persisted = run_async(qqbox._load_qq_data())
    assert len(persisted) == 20
    assert persisted["10000"]["nickname"] == "user-0"
    assert persisted["10019"]["nickname"] == "user-19"


def test_migrate_legacy_qq_data_imports_json_and_deletes_sources(qqbox) -> None:
    avatar_legacy_file = qqbox.avatar_image_path / "qq_data.json"
    qqbox.legacy_qq_data_files = [qqbox.qq_data_file, avatar_legacy_file]
    Path(qqbox.qq_data_file).write_text(
        '{"10001": {"nickname": "Amiya", "color": "3"}}',
        encoding="utf-8",
    )
    avatar_legacy_file.write_text(
        '{"10002": {"nickname": "Doctor"}}',
        encoding="utf-8",
    )

    run_async(qqbox._migrate_legacy_qq_data())

    assert run_async(qqbox._load_qq_data()) == {
        "10001": {
            "nickname": "Amiya",
            "color": 3,
            "content": None,
            "notes": None,
        },
        "10002": {
            "nickname": "Doctor",
            "color": None,
            "content": None,
            "notes": None,
        },
    }
    assert not Path(qqbox.qq_data_file).exists()
    assert not avatar_legacy_file.exists()


def test_migrate_legacy_qq_data_does_not_overwrite_existing_database(qqbox) -> None:
    qqbox.qq_title_key = {
        "10001": {
            "nickname": "Amiya",
            "color": 1,
            "content": "Leader",
            "notes": "Rabbit",
        }
    }
    run_async(qqbox._save_qq_data())

    Path(qqbox.qq_data_file).write_text(
        '{"10002": {"nickname": "Doctor"}, "10001": {"nickname": "Kaltsit"}}',
        encoding="utf-8",
    )

    run_async(qqbox._migrate_legacy_qq_data())

    assert run_async(qqbox._load_qq_data()) == {
        "10001": {
            "nickname": "Amiya",
            "color": 1,
            "content": "Leader",
            "notes": "Rabbit",
        },
        "10002": {
            "nickname": "Doctor",
            "color": None,
            "content": None,
            "notes": None,
        },
    }
    assert not Path(qqbox.qq_data_file).exists()


def test_migrate_legacy_qq_data_keeps_json_when_database_write_fails(
    qqbox, plugin_module, monkeypatch
) -> None:
    errors = []

    class LoggerStub:
        def error(self, message):
            errors.append(message)

    async def fail_save_missing(_profiles):
        raise sqlite3.OperationalError("database is locked")

    Path(qqbox.qq_data_file).write_text(
        '{"10001": {"nickname": "Amiya"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(qqbox.qq_profile_repo, "save_missing", fail_save_missing)
    monkeypatch.setattr(plugin_module, "logger", LoggerStub())

    run_async(qqbox._migrate_legacy_qq_data())

    assert Path(qqbox.qq_data_file).exists()
    assert any("database is locked" in message for message in errors)


def test_load_legacy_qq_data_returns_empty_dict_for_missing_empty_or_invalid_file(qqbox) -> None:
    assert qqbox._load_legacy_qq_data() == {}

    Path(qqbox.qq_data_file).write_text("   \n", encoding="utf-8")
    assert qqbox._load_legacy_qq_data() == {}

    Path(qqbox.qq_data_file).write_text("{not-json", encoding="utf-8")
    assert qqbox._load_legacy_qq_data() == {}


def test_get_legacy_qq_data_files_includes_known_old_locations(
    plugin_module, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", staticmethod(lambda: tmp_path))

    instance = plugin_module.QQbox(
        context=object(),
        config={
            "avatar_image_path": "./img/avatar",
            "bubble_font_path": "",
            "nickname_font_path": "",
            "title_font_path": "",
        },
    )

    assert instance.legacy_qq_data_files == [
        tmp_path / "qq_data.json",
        tmp_path / "avatars" / "qq_data.json",
        tmp_path / "img" / "avatar" / "qq_data.json",
    ]


def test_update_qq_title_key_merges_existing_values_and_persists(qqbox) -> None:
    qqbox.qq_title_key["10001"] = {
        "nickname": "Old",
        "color": 1,
        "content": "Captain",
        "notes": "Leader",
    }

    run_async(qqbox.update_qq_title_key("10001", color=4, notes="Rabbit"))

    assert qqbox.qq_title_key["10001"] == {
        "nickname": "Old",
        "color": 4,
        "content": "Captain",
        "notes": "Rabbit",
    }
    assert run_async(qqbox._load_qq_data())["10001"] == qqbox.qq_title_key["10001"]


def test_update_qq_title_key_keeps_memory_update_when_profile_write_fails(
    qqbox, plugin_module, monkeypatch
) -> None:
    errors = []

    class LoggerStub:
        def error(self, message):
            errors.append(message)

    async def fail_upsert(_qq, _profile):
        raise OSError("database is locked")

    monkeypatch.setattr(qqbox.qq_profile_repo, "upsert_profile", fail_upsert)
    monkeypatch.setattr(plugin_module, "logger", LoggerStub())

    run_async(qqbox.update_qq_title_key("10001", nickname="Amiya"))

    assert qqbox.qq_title_key["10001"]["nickname"] == "Amiya"
    assert any("database is locked" in message for message in errors)


def test_update_qq_title_key_logs_sqlite_write_failure(qqbox, plugin_module, monkeypatch) -> None:
    errors = []

    class LoggerStub:
        def error(self, message):
            errors.append(message)

    async def fail_upsert(_qq, _profile):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(qqbox.qq_profile_repo, "upsert_profile", fail_upsert)
    monkeypatch.setattr(plugin_module, "logger", LoggerStub())

    run_async(qqbox.update_qq_title_key("10001", nickname="Amiya"))

    assert qqbox.qq_title_key["10001"]["nickname"] == "Amiya"
    assert any("database is locked" in message for message in errors)


def test_get_nickname_by_api_tries_fallback_shapes_until_one_matches(qqbox) -> None:
    client = FakeAsyncClient(
        [
            FakeJsonResponse(payload={"status": "miss"}),
            FakeJsonResponse(payload={"name": "Amiya"}),
            FakeJsonResponse(payload={"nickname": "ShouldNotBeUsed"}),
        ]
    )

    nickname = run_async(qqbox.get_nickname_by_api("10001", client))

    assert nickname == "Amiya"
    assert len(client.calls) == 2
    assert client.calls[0][1] == 10.0


def test_get_nickname_by_api_falls_back_to_qq_when_all_requests_fail(qqbox) -> None:
    client = FakeAsyncClient(
        [
            httpx.RequestError("boom"),
            FakeJsonResponse(status_code=500, payload={}),
            FakeJsonResponse(payload={"status": "unknown"}),
        ]
    )

    assert run_async(qqbox.get_nickname_by_api("10001", client)) == "10001"
    assert len(client.calls) == 3


def test_download_circular_avatar_saves_processed_avatar(qqbox, tmp_path: Path, monkeypatch) -> None:
    client = FakeAsyncClient([FakeImageResponse(make_png_bytes())])
    save_path = tmp_path / "avatar.png"
    seen_sizes = []

    def fake_create_circular_avatar(image, size):
        seen_sizes.append((image.size, size))
        return Image.new("RGBA", (9, 9), (1, 2, 3, 255))

    monkeypatch.setattr(qqbox, "create_circular_avatar", fake_create_circular_avatar)

    success = run_async(
        qqbox.download_circular_avatar(
            "https://example.com/avatar.png",
            str(save_path),
            http_client=client,
            size=32,
        )
    )

    assert success is True
    assert save_path.exists()
    assert Image.open(save_path).size == (9, 9)
    assert seen_sizes == [((12, 12), 32)]


def test_download_circular_avatar_returns_false_for_missing_client_or_request_error(qqbox) -> None:
    assert (
        run_async(
            qqbox.download_circular_avatar(
                "https://example.com/avatar.png",
                "ignored.png",
                http_client=None,
            )
        )
        is False
    )

    client = FakeAsyncClient([httpx.RequestError("network down")])

    assert (
        run_async(
            qqbox.download_circular_avatar(
                "https://example.com/avatar.png",
                "ignored.png",
                http_client=client,
            )
        )
        is False
    )


def test_create_circular_avatar_centers_crop_and_masks_corners(qqbox) -> None:
    image = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    for x in range(6):
        for y in range(4):
            if x < 2:
                image.putpixel((x, y), (255, 0, 0, 255))
            elif x < 4:
                image.putpixel((x, y), (0, 255, 0, 255))
            else:
                image.putpixel((x, y), (0, 0, 255, 255))

    result = qqbox.create_circular_avatar(image, size=8)

    assert result.size == (8, 8)
    assert result.getpixel((0, 0))[3] == 0
    center = result.getpixel((4, 4))
    assert center[1] > center[0]
    assert center[1] > center[2]


def test_get_image_url_checks_direct_images_first(plugin_module) -> None:
    event = FakeEvent(images=[plugin_module.BotImage(url="https://direct.example/image.png")])

    assert (
        plugin_module.QQbox._get_image_url(plugin_module.QQbox.__new__(plugin_module.QQbox), event)
        == "https://direct.example/image.png"
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ([{"type": "image", "data": {"url": "https://dict.example/direct.png"}}], "https://dict.example/direct.png"),
        (
            [object(), {"type": "image", "url": "https://dict.example/fallback.png"}],
            "https://dict.example/fallback.png",
        ),
        (
            [plugin_module := None],
            None,
        ),
    ],
)
def test_get_image_url_from_message_shapes(plugin_module, message, expected) -> None:
    if message == [None]:
        message = [plugin_module.Reply(chain=[])]
    event = FakeEvent(message=message, images=[])

    assert (
        plugin_module.QQbox._get_image_url(plugin_module.QQbox.__new__(plugin_module.QQbox), event)
        == expected
    )


def test_get_image_url_reads_reply_chain(plugin_module) -> None:
    event = FakeEvent(
        message=[
            plugin_module.Reply(
                chain=[
                    {"type": "image", "data": {"url": "https://reply.example/from-dict.png"}}
                ]
            )
        ],
        images=[],
    )

    assert (
        plugin_module.QQbox._get_image_url(plugin_module.QQbox.__new__(plugin_module.QQbox), event)
        == "https://reply.example/from-dict.png"
    )


def test_safe_text_width_uses_textbbox_when_available(generator) -> None:
    class DrawStub:
        def textbbox(self, _pos, text, font):
            assert text == "Hello"
            assert font is not None
            return (0, 0, 37, 10)

    assert generator._safe_text_width(DrawStub(), "Hello", object(), 5) == 37


def test_safe_text_width_falls_back_for_missing_font_or_invalid_width(generator) -> None:
    class BrokenDraw:
        def textbbox(self, _pos, _text, font):
            if font is None:
                raise ValueError("font missing")
            return (0, 0, 0, 0)

    assert generator._safe_text_width(BrokenDraw(), "abc", None, 6) == 18
    assert generator._safe_text_width(BrokenDraw(), "abc", object(), 6) == 18
    assert generator._safe_text_width(BrokenDraw(), "", object(), 6) == 0


def test_text_units_keep_combining_zwj_and_punctuation_pairs_together(generator) -> None:
    assert generator._text_units("A\u0301✈️👩‍💻……——B") == [
        "A\u0301",
        "✈️",
        "👩‍💻",
        "……",
        "——",
        "B",
    ]


@pytest.mark.parametrize(
    "punctuation", tuple("，。！？；：、）】》」』”’·‼⁇⁈⁉")
)
def test_find_legal_break_keeps_closing_punctuation_off_line_start(
    generator, punctuation
) -> None:
    units = ["甲", punctuation, "乙"]
    assert generator._find_legal_break(units, 0, 1) == 2


@pytest.mark.parametrize("punctuation", tuple("（【《「『“‘〖〘〚"))
def test_find_legal_break_keeps_opening_punctuation_off_line_end(
    generator, punctuation
) -> None:
    units = ["甲", punctuation, "乙"]
    assert generator._find_legal_break(units, 0, 2) == 1


@pytest.mark.parametrize("solidus", ("/", "／"))
def test_find_legal_break_keeps_solidus_off_both_line_edges(
    generator, solidus
) -> None:
    units = ["甲", solidus, "乙"]

    assert generator._find_legal_break(units, 0, 1) == 3
    assert generator._find_legal_break(units, 0, 2) == 3


def test_find_legal_break_keeps_western_words_and_numbers_together(generator) -> None:
    assert generator._find_legal_break(list("甲AstrBot乙"), 0, 5) == 1
    assert generator._find_legal_break(list("AstrBot乙"), 0, 4) == 7
    assert generator._find_legal_break(list("甲2026乙"), 0, 3) == 1


def test_wrap_text_avoids_prohibited_punctuation_at_line_edges(
    generator, plugin_module, monkeypatch
) -> None:
    monkeypatch.setattr(generator, "max_width", 3)
    monkeypatch.setattr(generator, "bubble_padding", 0)
    monkeypatch.setattr(generator, "SCALE", 1)
    monkeypatch.setattr(
        generator,
        "_safe_text_width",
        lambda _draw, text, _font, _fallback: len(text),
    )

    lines = generator._wrap_text("今天好，继续（测试）", object())

    assert "".join(lines) == "今天好，继续（测试）"
    assert all(not line.startswith(tuple(plugin_module.PROHIBITED_LINE_START)) for line in lines)
    assert all(not line.endswith(tuple(plugin_module.PROHIBITED_LINE_END)) for line in lines)


def test_wrap_text_preserves_explicit_and_empty_lines(generator, monkeypatch) -> None:
    monkeypatch.setattr(generator, "max_width", 100)
    monkeypatch.setattr(generator, "bubble_padding", 0)
    monkeypatch.setattr(generator, "SCALE", 1)
    monkeypatch.setattr(
        generator,
        "_safe_text_width",
        lambda _draw, text, _font, _fallback: len(text),
    )

    assert generator._wrap_text("你好\n\n，世界", object()) == ["你好", "", "，世界"]


def test_wrap_text_layout_marks_each_paragraph_last_line(generator, monkeypatch) -> None:
    monkeypatch.setattr(generator, "max_width", 2)
    monkeypatch.setattr(generator, "bubble_padding", 0)
    monkeypatch.setattr(generator, "SCALE", 1)
    monkeypatch.setattr(
        generator,
        "_safe_text_width",
        lambda _draw, text, _font, _fallback: len(text),
    )

    assert generator._wrap_text_layout("甲乙丙\n丁戊", object()) == [
        ("甲乙", False),
        ("丙", True),
        ("丁戊", True),
    ]


def test_draw_text_line_distributes_only_adjustable_gaps(generator) -> None:
    class DrawStub:
        def __init__(self):
            self.calls = []

        def text(self, position, text, **_kwargs):
            self.calls.append((position, text))

    draw = DrawStub()
    generator._safe_text_width = lambda _draw, text, _font, _fallback: len(text) * 10

    generator._draw_text_line(
        draw,
        (5, 7),
        "Hello世界",
        object(),
        "black",
        target_width=100,
    )

    assert [text for _, text in draw.calls] == ["Hello", "世", "界"]
    assert [position[0] for position, _ in draw.calls] == [5, 70, 95]


def test_draw_text_line_keeps_paragraph_last_line_natural(generator) -> None:
    class DrawStub:
        def __init__(self):
            self.calls = []

        def text(self, position, text, **_kwargs):
            self.calls.append((position, text))

    draw = DrawStub()

    generator._draw_text_line(draw, (5, 7), "最后一行", object(), "black")

    assert draw.calls == [((5, 7), "最后一行")]


def test_draw_text_line_avoids_excessive_justification_gaps(generator) -> None:
    class DrawStub:
        def __init__(self):
            self.calls = []

        def text(self, position, text, **_kwargs):
            self.calls.append((position, text))

    draw = DrawStub()
    generator._safe_text_width = lambda _draw, text, _font, _fallback: len(text) * 10

    generator._draw_text_line(
        draw,
        (5, 7),
        "甲乙",
        object(),
        "black",
        target_width=200,
    )

    assert draw.calls == [((5, 7), "甲乙")]


def test_wrap_text_makes_progress_when_one_unit_is_too_wide(generator, monkeypatch) -> None:
    monkeypatch.setattr(generator, "max_width", 1)
    monkeypatch.setattr(generator, "bubble_padding", 0)
    monkeypatch.setattr(generator, "SCALE", 1)
    monkeypatch.setattr(generator, "_safe_text_width", lambda *_args: 10)

    assert generator._wrap_text("👩‍💻A", object()) == ["👩‍💻", "A"]


def test_wrap_text_minimally_overflows_instead_of_ending_with_opening_mark(
    generator, monkeypatch
) -> None:
    monkeypatch.setattr(generator, "max_width", 1)
    monkeypatch.setattr(generator, "bubble_padding", 0)
    monkeypatch.setattr(generator, "SCALE", 1)
    monkeypatch.setattr(
        generator,
        "_safe_text_width",
        lambda _draw, text, _font, _fallback: len(text),
    )

    assert generator._wrap_text("（甲", object()) == ["（甲"]


def test_render_uses_one_bundle_snapshot_during_hot_swap(generator, plugin_module) -> None:
    first = SimpleNamespace(
        bubble="first", nickname="first", title="first", title_scaled="first"
    )
    second = SimpleNamespace(
        bubble="second", nickname="second", title="second", title_scaled="second"
    )
    generator.install_font_bundle(first)
    observed = []

    def render(self):
        observed.append(self.bubble_font)
        self.install_font_bundle(second)
        observed.append(self.bubble_font)

    plugin_module._with_font_snapshot(render)(generator)

    assert observed == ["first", "first"]
    assert generator.bubble_font == "second"


def test_wrap_text_uses_safe_width_fallback_when_pillow_measurement_fails(
    generator, monkeypatch
) -> None:
    class BrokenDraw:
        def textbbox(self, *_args, **_kwargs):
            raise ValueError("missing glyph")

    monkeypatch.setattr(generator, "_get_temp_draw", lambda: BrokenDraw())
    monkeypatch.setattr(generator, "max_width", 20)
    monkeypatch.setattr(generator, "bubble_padding", 0)
    monkeypatch.setattr(generator, "SCALE", 1)
    generator._font_configs["bubble"] = ("", 2)

    lines = generator._wrap_text("真的吗？！继续", object())

    assert "".join(lines) == "真的吗？！继续"
    assert all(not line.startswith(("？", "！")) for line in lines)


def test_create_chat_message_requires_user_info(generator) -> None:
    with pytest.raises(ValueError):
        generator.create_chat_message("10001", "hello", None, user_info=None)


@pytest.mark.parametrize(
    ("text", "image", "expected_call"),
    [
        ("hello", None, ("text", "hello")),
        (None, Image.new("RGBA", (12, 12), (1, 2, 3, 255)), ("image", (12, 12))),
        (
            "hello",
            Image.new("RGBA", (12, 12), (1, 2, 3, 255)),
            ("text_image", ("hello", (12, 12))),
        ),
        (None, None, ("text", " ")),
    ],
)
def test_create_chat_message_selects_expected_bubble_builder(
    generator,
    sample_avatar: Path,
    monkeypatch,
    text,
    image,
    expected_call,
) -> None:
    calls = []
    bubble = Image.new("RGBA", (40, 20), (255, 255, 255, 255))

    def fake_text(value):
        calls.append(("text", value))
        return bubble

    def fake_image(value):
        calls.append(("image", value.size))
        return bubble

    def fake_text_image(value, picture):
        calls.append(("text_image", (value, picture.size)))
        return bubble

    def fake_background_size(bubble_image, nickname, title_info):
        assert bubble_image is bubble
        assert nickname == "Amiya"
        assert title_info is None
        return (140, 90)

    monkeypatch.setattr(generator, "create_chat_bubble", fake_text)
    monkeypatch.setattr(generator, "create_chat_img_bubble", fake_image)
    monkeypatch.setattr(generator, "create_chat_text_img_bubble", fake_text_image)
    monkeypatch.setattr(generator, "_calculate_background_size", fake_background_size)
    monkeypatch.setattr(
        generator,
        "_create_background_canvas",
        lambda width, height: Image.new("RGBA", (width, height), (240, 240, 242, 255)),
    )
    monkeypatch.setattr(generator, "_add_avatar", lambda background, avatar_path: None)
    monkeypatch.setattr(
        generator,
        "_add_name_and_title",
        lambda background, nickname, title_info: None,
    )

    payload = generator.create_chat_message(
        "10001",
        text,
        image,
        qq_title_key={},
        user_info={"name": "Amiya", "avatar_path": str(sample_avatar)},
    )

    assert payload.getvalue().startswith(b"\x89PNG")
    assert calls[0] == expected_call


def test_create_chat_message_uses_notes_as_display_name(generator, sample_avatar: Path, monkeypatch) -> None:
    seen = {}

    monkeypatch.setattr(
        generator,
        "create_chat_bubble",
        lambda value: Image.new("RGBA", (32, 18), (255, 255, 255, 255)),
    )
    monkeypatch.setattr(
        generator,
        "_calculate_background_size",
        lambda bubble, nickname, title_info: (seen.setdefault("nickname", nickname) and 120, 90),
    )
    monkeypatch.setattr(
        generator,
        "_create_background_canvas",
        lambda width, height: Image.new("RGBA", (width, height), (240, 240, 242, 255)),
    )
    monkeypatch.setattr(generator, "_add_avatar", lambda background, avatar_path: None)

    def capture_name(background, nickname, title_info) -> None:
        seen["draw_name"] = nickname
        seen["title_info"] = title_info

    monkeypatch.setattr(generator, "_add_name_and_title", capture_name)

    payload = generator.create_chat_message(
        "10001",
        "hello",
        None,
        qq_title_key={"10001": {"notes": "Rabbit", "content": "Leader", "color": 4}},
        user_info={"name": "Amiya", "avatar_path": str(sample_avatar)},
    )

    assert payload.getvalue().startswith(b"\x89PNG")
    assert seen["nickname"] == "Rabbit"
    assert seen["draw_name"] == "Rabbit"
    assert seen["title_info"]["content"] == "Leader"


def test_create_chat_message_by_gif_requires_user_info(generator) -> None:
    with pytest.raises(ValueError):
        generator.create_chat_message_by_gif("10001", None, make_gif_image(), user_info=None)


def test_create_chat_message_by_gif_returns_gif_with_multiple_frames(generator, sample_avatar: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        generator,
        "create_chat_img_bubble",
        lambda frame: Image.new("RGBA", (40, 24), (frame.getpixel((0, 0))[0], 0, 0, 255)),
    )
    monkeypatch.setattr(generator, "_calculate_background_size", lambda bubble, nickname, title_info: (140, 100))
    monkeypatch.setattr(
        generator,
        "_create_background_canvas",
        lambda width, height: Image.new("RGBA", (width, height), (240, 240, 242, 255)),
    )
    monkeypatch.setattr(generator, "_add_avatar", lambda background, avatar_path: None)
    monkeypatch.setattr(generator, "_add_name_and_title", lambda background, nickname, title_info: None)

    payload = generator.create_chat_message_by_gif(
        "10001",
        None,
        make_gif_image(),
        qq_title_key={},
        user_info={"name": "Amiya", "avatar_path": str(sample_avatar)},
    )

    assert payload.getvalue()[:6] in {b"GIF87a", b"GIF89a"}
    gif = Image.open(BytesIO(payload.getvalue()))
    assert getattr(gif, "n_frames", 1) == 2
    assert [frame.info.get("duration") for frame in ImageSequence.Iterator(gif)] == [20, 80]


def test_create_chat_message_by_gif_rejects_empty_frame_sequence(generator, monkeypatch) -> None:
    monkeypatch.setattr(ImageSequence, "Iterator", lambda _image: [])

    with pytest.raises(ValueError):
        generator.create_chat_message_by_gif(
            "10001",
            None,
            object(),
            qq_title_key={},
            user_info={"name": "Amiya", "avatar_path": None},
        )
