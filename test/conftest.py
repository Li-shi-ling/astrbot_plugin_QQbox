from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _LoggerStub:
    def debug(self, *args, **kwargs) -> None:
        return None

    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None

    def error(self, *args, **kwargs) -> None:
        return None


class _CommandGroup:
    def __init__(self, func):
        self.func = func

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def command(self, _name):
        def decorator(func):
            return func

        return decorator


class _FilterStub:
    def command_group(self, _name):
        def decorator(func):
            return _CommandGroup(func)

        return decorator


class _Star:
    def __init__(self, context) -> None:
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir() -> Path:
        return REPO_ROOT


class _BotImage:
    def __init__(self, url: str | None = None, file: str | None = None) -> None:
        self.url = url
        self.file = file
        self.payload = None

    @classmethod
    def fromBytes(cls, payload: bytes):
        image = cls()
        image.payload = payload
        return image


class _Reply:
    def __init__(self, chain=None) -> None:
        self.chain = chain or []


def _register(*_args, **_kwargs):
    def decorator(obj):
        return obj

    return decorator


def _install_astrbot_stubs() -> None:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    message_module = types.ModuleType("astrbot.api.message_components")
    star_module = types.ModuleType("astrbot.api.star")

    api_module.AstrBotConfig = dict
    api_module.logger = _LoggerStub()
    event_module.AstrMessageEvent = object
    event_module.filter = _FilterStub()
    message_module.Image = _BotImage
    message_module.Reply = _Reply
    star_module.Context = object
    star_module.Star = _Star
    star_module.StarTools = _StarTools
    star_module.register = _register

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.message_components"] = message_module
    sys.modules["astrbot.api.star"] = star_module


_install_astrbot_stubs()


class FakeEvent:
    def __init__(self, message=None, images=None) -> None:
        self.message_obj = SimpleNamespace(message=message or [])
        self._images = images

    def get_images(self):
        return self._images


@pytest.fixture
def plugin_module():
    sys.modules.pop("data.plugins.astrbot_plugin_QQbox.main", None)
    return importlib.import_module("data.plugins.astrbot_plugin_QQbox.main")


@pytest.fixture
def qqbox(plugin_module, tmp_path: Path):
    instance = plugin_module.QQbox.__new__(plugin_module.QQbox)
    instance.qq_data_file = tmp_path / "qq_data.json"
    instance.legacy_qq_data_files = [instance.qq_data_file]
    instance.qq_db_file = tmp_path / "db" / "qqbox.db"
    instance.qq_profile_repo = plugin_module.QQProfileRepo(instance.qq_db_file)
    instance.qq_title_key = {}
    instance.data_dir = tmp_path
    instance.avatar_image_path = tmp_path / "avatars"
    instance.bubble_font_path = str(tmp_path / "bubble.ttf")
    instance.nickname_font_path = str(tmp_path / "nickname.ttf")
    instance.title_font_path = str(tmp_path / "title.ttf")
    instance._font_paths_logged_on_failure = False
    instance.avatar_image_path.mkdir(parents=True, exist_ok=True)
    instance.http_client = None
    instance.font_manager = plugin_module.FontManager(
        tmp_path,
        Path(plugin_module.__file__).resolve().parent / "resources" / "font_manifest.json",
        plugin_module.FontConfig(auto_download=False),
    )
    return instance


@pytest.fixture
def generator(plugin_module, tmp_path: Path):
    instance = plugin_module.ChatBubbleGenerator(
        bubble_font_path="",
        nickname_font_path="",
        title_font_path="",
        avatar_image_path=str(tmp_path),
    )
    default_font = ImageFont.load_default()
    instance.install_font_bundle(
        SimpleNamespace(
            bubble=default_font,
            nickname=default_font,
            title=default_font,
            title_scaled=default_font,
        )
    )
    return instance


@pytest.fixture
def sample_avatar(tmp_path: Path) -> Path:
    avatar_path = tmp_path / "avatar.png"
    Image.new("RGBA", (24, 24), (20, 120, 220, 255)).save(avatar_path)
    return avatar_path
