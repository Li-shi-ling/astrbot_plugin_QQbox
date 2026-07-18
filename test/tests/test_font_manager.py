from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
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


def fake_bundle(paths, version):
    return types.SimpleNamespace(paths=paths, version=version)


def make_archive(*, unsafe: bool = False, omit: set[str] | None = None) -> bytes:
    omit = omit or set()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        members = {
            "SourceHanSansCN-ExtraLight.otf": b"extra",
            "SourceHanSansCN-Normal.otf": b"normal",
            "SourceHanSansCN-Bold.otf": b"bold",
            "LICENSE.txt": b"OFL",
        }
        for name, payload in members.items():
            if name not in omit:
                archive.writestr(f"OTF/{name}", payload)
        if unsafe:
            archive.writestr("../escaped.txt", b"bad")
    return output.getvalue()


def write_manifest(
    path: Path,
    archive: bytes,
    *,
    digest: str | None = None,
    max_bytes: int = 1024 * 1024,
) -> Path:
    payload = {
        "source_repository": "adobe-fonts/source-han-sans",
        "pack_version": "2.005R-cn",
        "url": "https://github.com/adobe-fonts/source-han-sans/releases/download/2.005R/19_SourceHanSansCN.zip",
        "archive_sha256": digest or hashlib.sha256(archive).hexdigest(),
        "max_bytes": max_bytes,
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
    manager._load_bundle = fake_bundle
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


def test_download_never_writes_to_plugin_directory(tmp_path: Path) -> None:
    archive = make_archive()
    plugin_resources = tmp_path / "plugin" / "resources"
    plugin_resources.mkdir(parents=True)
    manifest = write_manifest(plugin_resources / "font_manifest.json", archive)
    before = {path.relative_to(plugin_resources): path.read_bytes() for path in plugin_resources.rglob("*") if path.is_file()}

    async def downloader(_url, destination, _progress):
        destination.write_bytes(archive)

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )
    manager._validate_default_fonts = lambda _paths: None
    manager._load_bundle = fake_bundle

    run_async(prepare(manager))

    after = {path.relative_to(plugin_resources): path.read_bytes() for path in plugin_resources.rglob("*") if path.is_file()}
    assert after == before
    assert manager.version_dir.is_relative_to(tmp_path / "persistent")


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
    expected = types.SimpleNamespace(version="2.005R-cn")
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

    mixed, mixed_calls = make_manager(
        tmp_path,
        archive,
        bubble_path=str(custom),
    )
    run_async(prepare(mixed))
    assert mixed_calls == []
    assert mixed.get_bundle().paths.bubble == custom.resolve()
    assert mixed.get_bundle().paths.nickname.is_relative_to(mixed.font_root)
    assert mixed.get_bundle().paths.nickname.name == mixed.manifest.files["normal"]


def test_previous_version_cache_survives_new_download_failure(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)

    async def downloader(_url, _destination, _progress):
        raise FontUnsupportedError("offline")

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )
    manager._load_bundle = fake_bundle
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
    assert manager.get_bundle().version == "2.004R-cn"
    assert manager.status().cache_path == old_dir
    assert manager.needs_update is True

    async def successful_download(_url, destination, _progress):
        destination.write_bytes(archive)

    manager._downloader = successful_download

    async def retry():
        await manager.retry()

    run_async(retry())

    assert manager.get_bundle().version == "2.005R-cn"
    assert manager.needs_update is False


def test_invalid_custom_path_fails_without_default_fallback(tmp_path: Path) -> None:
    manager, calls = make_manager(
        tmp_path, make_archive(), bubble_path=str(tmp_path / "missing.otf")
    )

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_CONFIG
    assert calls == []


def test_relative_custom_path_is_rejected_without_using_cwd(tmp_path: Path) -> None:
    manager, calls = make_manager(
        tmp_path, make_archive(), bubble_path="relative/custom.otf"
    )

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_CONFIG
    assert "绝对路径" in manager.status().error
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


def test_oversized_archive_is_rejected(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(
        tmp_path / "manifest.json", archive, max_bytes=len(archive) - 1
    )

    async def downloader(_url, destination, _progress):
        destination.write_bytes(archive)

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_VERIFY
    assert "大小异常" in manager.status().error


def test_transient_download_errors_retry_then_install(
    tmp_path: Path, monkeypatch
) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)
    attempts = 0

    async def downloader(_url, destination, _progress):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary")
        destination.write_bytes(archive)

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(font_manager_module.asyncio, "sleep", no_delay)
    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )
    manager._validate_default_fonts = lambda _paths: None
    manager._load_bundle = fake_bundle

    run_async(prepare(manager))

    assert attempts == 3
    assert manager.status().state is FontState.READY


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    manager, _calls = make_manager(tmp_path, make_archive(unsafe=True))

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_VERIFY
    assert not (tmp_path / "persistent" / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize(
    "missing",
    ["SourceHanSansCN-Normal.otf", "LICENSE.txt"],
)
def test_archive_missing_required_font_or_license_is_rejected(
    tmp_path: Path, missing: str
) -> None:
    manager, _calls = make_manager(tmp_path, make_archive(omit={missing}))

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_VERIFY
    assert "缺少文件" in manager.status().error


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


def test_unloadable_current_cache_is_redownloaded(tmp_path: Path) -> None:
    archive = make_archive()
    manager, calls = make_manager(tmp_path, archive)
    manager.version_dir.mkdir(parents=True)
    for filename in manager.manifest.files.values():
        (manager.version_dir / filename).write_bytes(b"stale")
    (manager.version_dir / "installed.json").write_text(
        json.dumps(
            {
                "pack_version": manager.manifest.pack_version,
                "archive_sha256": manager.manifest.archive_sha256,
            }
        ),
        encoding="utf-8",
    )

    def validate(paths):
        if paths.bubble.read_bytes() == b"stale":
            raise FontVerifyError("broken cache")

    manager._validate_default_fonts = validate

    run_async(prepare(manager))

    assert manager.status().state is FontState.READY
    assert len(calls) == 1
    assert manager.get_bundle().paths.bubble.read_bytes() == b"normal"


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
        manager._load_bundle = fake_bundle
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


def test_two_manager_instances_share_one_download(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive)

    async def scenario():
        calls = 0

        async def downloader(_url, destination, _progress):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            destination.write_bytes(archive)

        managers = [
            FontManager(
                tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
            )
            for _ in range(2)
        ]
        for manager in managers:
            manager._validate_default_fonts = lambda _paths: None
            manager._load_bundle = fake_bundle
        await asyncio.gather(*(manager.start() for manager in managers))
        assert calls == 1
        assert all(manager.status().state is FontState.READY for manager in managers)

    run_async(scenario())


def test_active_cross_process_lock_is_waited_for(tmp_path: Path) -> None:
    manager, calls = make_manager(tmp_path, make_archive())
    manager.font_root.mkdir(parents=True)
    manager.lock_path.write_text(
        json.dumps({"pid": 1, "time": 9999999999, "token": "active"}),
        encoding="utf-8",
    )

    async def scenario():
        async def release_lock():
            await asyncio.sleep(0.05)
            manager.lock_path.unlink()

        release_task = asyncio.create_task(release_lock())
        await manager.start()
        await release_task

    run_async(scenario())

    assert len(calls) == 1
    assert manager.status().state is FontState.READY


def test_stale_cross_process_lock_is_removed(tmp_path: Path, monkeypatch) -> None:
    manager, _calls = make_manager(tmp_path, make_archive())
    manager.font_root.mkdir(parents=True)
    manager.lock_path.write_text(
        json.dumps({"pid": 999999, "time": 0, "token": "old"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "_pid_exists", lambda _pid: False)

    assert manager._remove_stale_lock() is True
    assert not manager.lock_path.exists()


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
        nickname=tmp_path / "SourceHanSansCN-Normal.otf",
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


def test_background_failure_is_consumed_and_recorded(tmp_path: Path) -> None:
    manager, _calls = make_manager(tmp_path, make_archive())
    manager._load_bundle = lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))

    async def scenario():
        task = manager.start()
        await task
        assert task.exception() is None

    run_async(scenario())

    assert manager.status().state is FontState.FAILED_LOAD
    assert manager.status().error == "boom"


def test_download_does_not_overwrite_astrbot_proxy_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080")
    manager, _calls = make_manager(tmp_path, make_archive())

    run_async(prepare(manager))

    assert os.environ["http_proxy"] == "http://proxy.example:8080"
    assert os.environ["https_proxy"] == "http://proxy.example:8080"


def test_offline_first_start_reports_failure_without_raising(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "manifest.json", make_archive())

    async def offline(_url, _destination, _progress):
        raise FontUnsupportedError("offline")

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=offline
    )

    run_async(prepare(manager))

    assert manager.status().state is FontState.FAILED_UNSUPPORTED
