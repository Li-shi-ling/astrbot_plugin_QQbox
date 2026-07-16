from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_QQbox.src import font_manager as font_manager_module
from data.plugins.astrbot_plugin_QQbox.src.font_manager import (
    FontConfig,
    FontManager,
    FontPaths,
    FontState,
    FontUnsupportedError,
    FontVerifyError,
)


def run_async(coro):
    return asyncio.run(coro)


async def prepare(manager: FontManager) -> None:
    await manager.start()


def make_archive(*, unsafe: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("OTF/SourceHanSansCN-ExtraLight.otf", b"extra")
        archive.writestr("OTF/SourceHanSansCN-Normal.otf", b"normal")
        archive.writestr("OTF/SourceHanSansCN-Bold.otf", b"bold")
        archive.writestr("LICENSE.txt", b"OFL")
        if unsafe:
            archive.writestr("../escaped.txt", b"bad")
    return output.getvalue()


def write_manifest(path: Path, archive: bytes, *, digest: str | None = None) -> Path:
    payload = {
        "source_repository": "adobe-fonts/source-han-sans",
        "pack_version": "2.005R-cn",
        "url": "https://github.com/adobe-fonts/source-han-sans/releases/download/2.005R/19_SourceHanSansCN.zip",
        "archive_sha256": digest or hashlib.sha256(archive).hexdigest(),
        "max_bytes": 1024 * 1024,
        "files": {
            "extra_light": "SourceHanSansCN-ExtraLight.otf",
            "normal": "SourceHanSansCN-Normal.otf",
            "bold": "SourceHanSansCN-Bold.otf",
            "license": "LICENSE.txt",
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_manager(tmp_path: Path, archive: bytes, **config_values):
    manifest = write_manifest(tmp_path / "manifest.json", archive)
    calls = []

    async def downloader(url, destination, progress):
        calls.append((url, destination))
        destination.write_bytes(archive)
        progress({"downloaded": len(archive), "total": len(archive)})

    manager = FontManager(
        tmp_path / "persistent",
        manifest,
        FontConfig(**config_values),
        downloader=downloader,
    )
    manager._load_bundle = lambda paths, version: (paths, version)
    manager._validate_default_fonts = lambda _paths: None
    return manager, calls


def test_download_installs_only_inside_persistent_font_root(tmp_path: Path) -> None:
    archive = make_archive()
    manager, calls = make_manager(tmp_path, archive)

    run_async(prepare(manager))

    assert manager.status().state is FontState.READY
    assert manager.status().downloaded == len(archive)
    assert len(calls) == 1
    assert calls[0][1].is_relative_to(manager.font_root)
    assert manager.version_dir.is_dir()
    assert (manager.version_dir / "installed.json").is_file()
    assert not (manager.staging_dir / "19_SourceHanSansCN.zip.part").exists()
    assert all(path.is_relative_to(manager.font_root) for path in manager._owned_staging)


def test_complete_bundle_is_published_through_single_ready_callback(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)
    installed = []

    async def downloader(_url, destination, _progress):
        destination.write_bytes(archive)

    manager = FontManager(
        tmp_path / "persistent",
        manifest,
        FontConfig(),
        downloader=downloader,
        on_ready=installed.append,
    )
    expected = object()
    manager._load_bundle = lambda _paths, _version: expected
    manager._validate_default_fonts = lambda _paths: None

    run_async(prepare(manager))

    assert installed == [expected]
    assert manager.get_bundle() is expected
    assert manager.status().state is FontState.READY


def test_cache_hit_and_custom_paths_do_not_download(tmp_path: Path) -> None:
    archive = make_archive()
    manager, calls = make_manager(tmp_path, archive)
    run_async(prepare(manager))
    assert len(calls) == 1

    second, second_calls = make_manager(tmp_path, archive)
    run_async(prepare(second))
    assert second.status().state is FontState.READY
    assert second_calls == []

    custom = tmp_path / "custom.otf"
    custom.write_bytes(b"font")
    third, third_calls = make_manager(
        tmp_path,
        archive,
        bubble_path=str(custom),
        nickname_path=str(custom),
        title_path=str(custom),
    )
    run_async(prepare(third))
    assert third.status().state is FontState.READY
    assert third_calls == []


def test_previous_version_cache_survives_new_download_failure(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)

    async def downloader(_url, _destination, _progress):
        raise FontUnsupportedError("offline")

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )
    manager._load_bundle = lambda paths, version: (paths, version)
    manager._validate_default_fonts = lambda _paths: None
    old_dir = manager.font_root / "2.004R-cn"
    old_dir.mkdir(parents=True)
    for filename in manager.manifest.files.values():
        (old_dir / filename).write_bytes(b"cached")
    (old_dir / "installed.json").write_text(
        json.dumps({"pack_version": "2.004R-cn", "archive_sha256": "old"}),
        encoding="utf-8",
    )

    run_async(prepare(manager))

    assert manager.status().state is FontState.READY
    assert manager.get_bundle()[1] == "2.004R-cn"


def test_invalid_custom_path_fails_without_default_fallback(tmp_path: Path) -> None:
    manager, calls = make_manager(
        tmp_path, make_archive(), bubble_path=str(tmp_path / "missing.otf")
    )

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_CONFIG
    assert calls == []


def test_mirror_prefix_changes_transport_url_only(tmp_path: Path) -> None:
    manager, calls = make_manager(
        tmp_path, make_archive(), github_mirror="https://mirror.example/"
    )

    run_async(prepare(manager))

    assert calls[0][0].startswith(
        "https://mirror.example/https://github.com/adobe-fonts/source-han-sans/"
    )


def test_astrbot_download_adapter_disables_insecure_tls_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    manager, _calls = make_manager(tmp_path, make_archive())
    captured = {}
    io_module = types.ModuleType("astrbot.core.utils.io")

    async def download_file(url, path, **kwargs):
        captured.update(url=url, path=path, kwargs=kwargs)

    io_module.download_file = download_file
    core_module = types.ModuleType("astrbot.core")
    utils_module = types.ModuleType("astrbot.core.utils")
    monkeypatch.setitem(sys.modules, "astrbot.core", core_module)
    monkeypatch.setitem(sys.modules, "astrbot.core.utils", utils_module)
    monkeypatch.setitem(sys.modules, "astrbot.core.utils.io", io_module)
    destination = manager.staging_dir / "test.part"

    run_async(manager._astrbot_download("https://example.invalid/font", destination, lambda _p: None))

    assert captured["path"] == str(destination)
    assert captured["kwargs"]["allow_insecure_ssl_fallback"] is False
    assert callable(captured["kwargs"]["progress_callback"])


def test_hash_mismatch_is_rejected_and_not_installed(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive, digest="0" * 64)

    async def downloader(_url, destination, _progress):
        destination.write_bytes(archive)

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )
    manager._validate_default_fonts = lambda _paths: None

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_VERIFY
    assert not manager.version_dir.exists()
    assert not (manager.staging_dir / "19_SourceHanSansCN.zip.part").exists()


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    manager, _calls = make_manager(tmp_path, make_archive(unsafe=True))

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_VERIFY
    assert not (tmp_path / "persistent" / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_invalid_existing_version_is_replaced_atomically(tmp_path: Path) -> None:
    archive = make_archive()
    manager, calls = make_manager(tmp_path, archive)
    manager.version_dir.mkdir(parents=True)
    (manager.version_dir / "stale.txt").write_text("old", encoding="utf-8")

    run_async(prepare(manager))

    assert manager.status().state is FontState.READY
    assert len(calls) == 1
    assert not (manager.version_dir / "stale.txt").exists()
    assert (manager.version_dir / "SourceHanSansCN-Normal.otf").read_bytes() == b"normal"


def test_start_is_non_blocking_and_reuses_single_task(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)

    async def scenario():
        release = asyncio.Event()
        calls = 0

        async def downloader(_url, destination, _progress):
            nonlocal calls
            calls += 1
            await release.wait()
            destination.write_bytes(archive)

        manager = FontManager(
            tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
        )
        manager._load_bundle = lambda paths, version: (paths, version)
        manager._validate_default_fonts = lambda _paths: None
        first = manager.start()
        second = manager.start()
        await asyncio.sleep(0)
        assert first is second
        assert not first.done()
        release.set()
        await first
        assert calls == 1

    run_async(scenario())


def test_font_root_does_not_depend_on_current_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    archive = make_archive()
    manager, _calls = make_manager(tmp_path, archive)
    expected = (tmp_path / "persistent" / "fonts").resolve()
    other = tmp_path / "cwd"
    other.mkdir()
    monkeypatch.chdir(other)

    assert manager.font_root == expected
    assert manager.version_dir.is_relative_to(expected)


def test_cleanup_rejects_paths_outside_font_root(tmp_path: Path) -> None:
    manager, _calls = make_manager(tmp_path, make_archive())
    outside = tmp_path / "do-not-delete"
    outside.write_text("safe", encoding="utf-8")
    manager._owned_staging.add(outside)

    try:
        manager._cleanup_owned_staging()
    except Exception:
        pass

    assert outside.read_text(encoding="utf-8") == "safe"


def test_default_font_family_and_weight_are_verified(
    tmp_path: Path, monkeypatch
) -> None:
    paths = FontPaths(
        bubble=tmp_path / "SourceHanSansCN-Normal.otf",
        nickname=tmp_path / "SourceHanSansCN-ExtraLight.otf",
        title=tmp_path / "SourceHanSansCN-Bold.otf",
    )

    class FakeFont:
        def __init__(self, name):
            self.name = name

        def getname(self):
            style = self.name.removeprefix("SourceHanSansCN-").removesuffix(".otf")
            return "Source Han Sans CN", style

    monkeypatch.setattr(
        font_manager_module.ImageFont,
        "truetype",
        lambda path, _size: FakeFont(Path(path).name),
    )
    FontManager._validate_default_fonts(paths)

    monkeypatch.setattr(
        font_manager_module.ImageFont,
        "truetype",
        lambda _path, _size: type("WrongFont", (), {"getname": lambda self: ("Other", "Bold")})(),
    )
    with pytest.raises(FontVerifyError, match="字体家族不匹配"):
        FontManager._validate_default_fonts(paths)


def test_close_cancels_download_and_cleans_owned_staging(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)

    async def scenario():
        started = asyncio.Event()

        async def downloader(_url, destination, _progress):
            destination.write_bytes(b"partial")
            started.set()
            await asyncio.Event().wait()

        manager = FontManager(
            tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
        )
        manager.start()
        await started.wait()
        await manager.close()
        assert manager.status().state is FontState.STOPPED
        assert not (manager.staging_dir / "19_SourceHanSansCN.zip.part").exists()

    run_async(scenario())
