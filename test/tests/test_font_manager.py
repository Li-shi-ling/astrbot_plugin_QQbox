from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

from data.plugins.astrbot_plugin_QQbox.src.font_manager import (
    FontConfig,
    FontManager,
    FontState,
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


def test_hash_mismatch_is_rejected_and_not_installed(tmp_path: Path) -> None:
    archive = make_archive()
    manifest = write_manifest(tmp_path / "manifest.json", archive, digest="0" * 64)

    async def downloader(_url, destination, _progress):
        destination.write_bytes(archive)

    manager = FontManager(
        tmp_path / "persistent", manifest, FontConfig(), downloader=downloader
    )

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
