from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from PIL import ImageFont


DownloadFunction = Callable[[str, Path, Callable[[dict], None]], Awaitable[None]]
_ROOT_LOCKS: dict[Path, asyncio.Lock] = {}


class FontState(str, Enum):
    NOT_STARTED = "not_started"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    LOADING = "loading"
    READY = "ready"
    FAILED_CONFIG = "failed_config"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_VERIFY = "failed_verify"
    FAILED_LOAD = "failed_load"
    FAILED_UNSUPPORTED = "failed_unsupported"
    STOPPED = "stopped"


@dataclass(frozen=True)
class FontConfig:
    bubble_path: str = ""
    nickname_path: str = ""
    title_path: str = ""
    auto_download: bool = True
    github_mirror: str = ""


@dataclass(frozen=True)
class FontPaths:
    bubble: Path
    nickname: Path
    title: Path


@dataclass(frozen=True)
class FontBundle:
    bubble: ImageFont.FreeTypeFont
    nickname: ImageFont.FreeTypeFont
    title: ImageFont.FreeTypeFont
    title_scaled: ImageFont.FreeTypeFont
    paths: FontPaths
    version: str


InstallFunction = Callable[[FontBundle], None]


@dataclass(frozen=True)
class FontStatus:
    state: FontState
    version: str
    cache_path: Path
    error: str | None
    downloaded: int
    total: int


@dataclass(frozen=True)
class FontManifest:
    source_repository: str
    pack_version: str
    url: str
    archive_sha256: str
    max_bytes: int
    files: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> FontManifest:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = cls(
            source_repository=str(payload["source_repository"]),
            pack_version=str(payload["pack_version"]),
            url=str(payload["url"]),
            archive_sha256=str(payload["archive_sha256"]).lower(),
            max_bytes=int(payload["max_bytes"]),
            files={str(key): str(value) for key, value in payload["files"].items()},
        )
        required = {"extra_light", "normal", "bold", "license"}
        if set(manifest.files) != required:
            raise ValueError("字体清单文件角色不完整")
        if any(
            Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            for filename in manifest.files.values()
        ):
            raise ValueError("字体清单文件名不得包含目录")
        if manifest.source_repository != "adobe-fonts/source-han-sans":
            raise ValueError("字体清单来源不是 Adobe 官方仓库")
        if not manifest.url.startswith(
            "https://github.com/adobe-fonts/source-han-sans/releases/download/"
        ):
            raise ValueError("字体清单 URL 不是 Adobe 官方 Release")
        return manifest


class FontManagerError(RuntimeError):
    pass


class FontConfigError(FontManagerError):
    pass


class FontDownloadError(FontManagerError):
    pass


class FontVerifyError(FontManagerError):
    pass


class FontUnsupportedError(FontManagerError):
    pass


class FontManager:
    def __init__(
        self,
        data_dir: Path,
        manifest_path: Path,
        config: FontConfig,
        *,
        bubble_size: int = 34,
        nickname_size: int = 25,
        title_size: int = 19,
        scale: int = 4,
        downloader: DownloadFunction | None = None,
        on_ready: InstallFunction | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.font_root = (self.data_dir / "fonts").resolve()
        self.manifest = FontManifest.load(Path(manifest_path))
        self.config = config
        self.bubble_size = bubble_size
        self.nickname_size = nickname_size
        self.title_size = title_size
        self.scale = scale
        self._downloader = downloader or self._astrbot_download
        self._on_ready = on_ready
        self._state = FontState.NOT_STARTED
        self._error: str | None = None
        self._downloaded = 0
        self._total = 0
        self._bundle: FontBundle | None = None
        self._prepare_task: asyncio.Task[None] | None = None
        self._ready_event = asyncio.Event()
        self._owned_staging: set[Path] = set()
        self._file_lock_token: str | None = None

    @property
    def version_dir(self) -> Path:
        return self._inside(self.font_root / self.manifest.pack_version)

    @property
    def staging_dir(self) -> Path:
        return self._inside(self.font_root / ".staging")

    @property
    def lock_path(self) -> Path:
        return self._inside(self.font_root / ".download.lock")

    def start(self) -> asyncio.Task[None]:
        if self._prepare_task is None or self._prepare_task.done():
            self._prepare_task = asyncio.create_task(self._prepare())
            self._prepare_task.add_done_callback(self._consume_task_result)
        return self._prepare_task

    def retry(self) -> asyncio.Task[None]:
        if self._prepare_task is not None and not self._prepare_task.done():
            return self._prepare_task
        if self._state is FontState.READY and self._prepare_task is not None:
            return self._prepare_task
        self._error = None
        self._ready_event.clear()
        return self.start()

    def status(self) -> FontStatus:
        return FontStatus(
            state=self._state,
            version=self.manifest.pack_version,
            cache_path=self.version_dir,
            error=self._error,
            downloaded=self._downloaded,
            total=self._total,
        )

    def get_bundle(self) -> FontBundle | None:
        return self._bundle

    async def wait_ready(self) -> None:
        await self._ready_event.wait()

    async def close(self) -> None:
        task = self._prepare_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._cleanup_owned_staging)
        await asyncio.to_thread(self._release_file_lock)
        self._state = FontState.STOPPED
        self._ready_event.clear()

    async def _prepare(self) -> None:
        self._ready_event.clear()
        self._error = None
        try:
            self._state = FontState.CHECKING
            configured = self._configured_paths()
            needs_default = any(path is None for path in configured.values())
            cache_dir = self.version_dir
            if needs_default and not await asyncio.to_thread(
                self._cache_valid, cache_dir, True
            ):
                if not self.config.auto_download:
                    raise FontDownloadError("默认字体缺失，且自动下载已关闭")
                try:
                    await self._ensure_cache()
                except (FontDownloadError, FontVerifyError, FontUnsupportedError):
                    fallback = await asyncio.to_thread(self._find_previous_cache)
                    if fallback is None:
                        raise
                    cache_dir = fallback

            paths = self._compose_paths(configured, cache_dir)
            self._state = FontState.LOADING
            bundle = await asyncio.to_thread(self._load_bundle, paths, cache_dir.name)
            if self._on_ready is not None:
                self._on_ready(bundle)
            self._bundle = bundle
            self._state = FontState.READY
            self._ready_event.set()
        except asyncio.CancelledError:
            raise
        except FontConfigError as exc:
            self._fail(FontState.FAILED_CONFIG, exc)
        except FontUnsupportedError as exc:
            self._fail(FontState.FAILED_UNSUPPORTED, exc)
        except FontVerifyError as exc:
            self._fail(FontState.FAILED_VERIFY, exc)
        except FontDownloadError as exc:
            self._fail(FontState.FAILED_DOWNLOAD, exc)
        except Exception as exc:
            self._fail(FontState.FAILED_LOAD, exc)

    def _configured_paths(self) -> dict[str, Path | None]:
        result: dict[str, Path | None] = {}
        for role, value in {
            "bubble": self.config.bubble_path,
            "nickname": self.config.nickname_path,
            "title": self.config.title_path,
        }.items():
            if not value or not value.strip():
                result[role] = None
                continue
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise FontConfigError(f"{role} 字体配置路径不存在: {path}")
            result[role] = path
        return result

    def _compose_paths(
        self, configured: dict[str, Path | None], cache_dir: Path
    ) -> FontPaths:
        return FontPaths(
            bubble=configured["bubble"]
            or cache_dir / self.manifest.files["normal"],
            nickname=configured["nickname"]
            or cache_dir / self.manifest.files["extra_light"],
            title=configured["title"] or cache_dir / self.manifest.files["bold"],
        )

    async def _ensure_cache(self) -> None:
        self.font_root.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        root_lock = _ROOT_LOCKS.setdefault(self.font_root, asyncio.Lock())
        async with root_lock:
            await self._acquire_file_lock()
            try:
                if await asyncio.to_thread(self._cache_valid, self.version_dir, True):
                    return
                part_path = self._inside(
                    self.staging_dir / "19_SourceHanSansCN.zip.part"
                )
                self._owned_staging.add(part_path)
                await self._download_with_retries(part_path)
                self._state = FontState.VERIFYING
                await asyncio.to_thread(self._verify_archive, part_path)
                install_dir = self._inside(
                    self.staging_dir / f"install-{uuid.uuid4().hex}"
                )
                self._owned_staging.add(install_dir)
                await asyncio.to_thread(self._extract_and_install, part_path, install_dir)
            except Exception:
                await asyncio.to_thread(self._cleanup_owned_staging)
                raise
            finally:
                await asyncio.to_thread(self._release_file_lock)

    async def _download_with_retries(self, part_path: Path) -> None:
        self._state = FontState.DOWNLOADING
        mirror = self.config.github_mirror.strip().rstrip("/")
        url = f"{mirror}/{self.manifest.url}" if mirror else self.manifest.url
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                part_path.unlink(missing_ok=True)
                await self._downloader(url, part_path, self._on_progress)
                if not part_path.is_file():
                    raise FontDownloadError("下载函数未生成字体归档")
                return
            except asyncio.CancelledError:
                raise
            except FontUnsupportedError:
                raise
            except Exception as exc:
                last_error = exc
                part_path.unlink(missing_ok=True)
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise FontDownloadError(
            "字体下载失败，请检查 AstrBot 网络代理或 GitHub 镜像配置"
        ) from last_error

    async def _astrbot_download(
        self, url: str, path: Path, progress: Callable[[dict], None]
    ) -> None:
        try:
            from astrbot.core.utils.io import download_file
        except ImportError as exc:
            raise FontUnsupportedError("当前 AstrBot 不提供异步下载接口，请升级 AstrBot") from exc
        await download_file(
            url,
            str(path),
            progress_callback=progress,
            allow_insecure_ssl_fallback=False,
        )

    def _on_progress(self, payload: dict) -> None:
        self._downloaded = int(payload.get("downloaded", 0) or 0)
        self._total = int(payload.get("total", 0) or 0)

    def _verify_archive(self, path: Path) -> None:
        size = path.stat().st_size
        if size <= 0 or size > self.manifest.max_bytes:
            raise FontVerifyError(f"字体归档大小异常: {size} bytes")
        digest = hashlib.sha256()
        with path.open("rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != self.manifest.archive_sha256:
            raise FontVerifyError("字体归档 SHA-256 校验失败")

    def _extract_and_install(self, archive_path: Path, install_dir: Path) -> None:
        install_dir.mkdir(parents=True, exist_ok=False)
        expected = set(self.manifest.files.values())
        found: set[str] = set()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    normalized = PurePosixPath(info.filename.replace("\\", "/"))
                    if normalized.is_absolute() or ".." in normalized.parts:
                        raise FontVerifyError("字体归档包含不安全路径")
                    filename = normalized.name
                    if info.is_dir() or filename not in expected or filename in found:
                        continue
                    destination = self._inside(install_dir / filename)
                    with archive.open(info) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    found.add(filename)
            missing = expected - found
            if missing:
                raise FontVerifyError(f"字体归档缺少文件: {', '.join(sorted(missing))}")
            paths = self._compose_paths(
                {"bubble": None, "nickname": None, "title": None}, install_dir
            )
            self._validate_default_fonts(paths)
            metadata = {
                "source_repository": self.manifest.source_repository,
                "pack_version": self.manifest.pack_version,
                "archive_sha256": self.manifest.archive_sha256,
                "installed_at": int(time.time()),
            }
            (install_dir / "installed.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            old_dir = None
            if self.version_dir.exists():
                old_dir = self._inside(
                    self.staging_dir / f"replaced-{uuid.uuid4().hex}"
                )
                self.version_dir.replace(old_dir)
            try:
                install_dir.replace(self.version_dir)
            except Exception:
                if old_dir is not None and old_dir.exists():
                    old_dir.replace(self.version_dir)
                raise
            if old_dir is not None:
                shutil.rmtree(old_dir, ignore_errors=True)
        finally:
            archive_path.unlink(missing_ok=True)
            self._owned_staging.discard(archive_path)
            if install_dir.exists():
                shutil.rmtree(install_dir, ignore_errors=True)
            self._owned_staging.discard(install_dir)

    def _load_bundle(self, paths: FontPaths, version: str) -> FontBundle:
        for path in (paths.bubble, paths.nickname, paths.title):
            if not path.is_file():
                raise FileNotFoundError(f"字体文件不存在: {path}")
        bubble = ImageFont.truetype(str(paths.bubble), self.bubble_size * self.scale)
        nickname = ImageFont.truetype(str(paths.nickname), self.nickname_size)
        title = ImageFont.truetype(str(paths.title), self.title_size)
        title_scaled = ImageFont.truetype(
            str(paths.title), self.title_size * self.scale
        )
        return FontBundle(
            bubble=bubble,
            nickname=nickname,
            title=title,
            title_scaled=title_scaled,
            paths=paths,
            version=version,
        )

    @staticmethod
    def _validate_default_fonts(paths: FontPaths) -> None:
        expected_styles = {
            paths.bubble: {"normal", "regular"},
            paths.nickname: {"extralight"},
            paths.title: {"bold"},
        }
        for path, accepted_styles in expected_styles.items():
            try:
                font = ImageFont.truetype(str(path), 16)
                family, style = font.getname()
            except Exception as exc:
                raise FontVerifyError(f"默认字体无法加载: {path.name}") from exc
            normalized_family = "".join(char for char in family.lower() if char.isalnum())
            normalized_style = "".join(char for char in style.lower() if char.isalnum())
            if "sourcehansanscn" not in normalized_family:
                raise FontVerifyError(f"默认字体家族不匹配: {path.name}")
            if normalized_style not in accepted_styles:
                raise FontVerifyError(f"默认字体字重不匹配: {path.name}")

    def _cache_valid(self, directory: Path, require_current: bool) -> bool:
        try:
            metadata = json.loads(
                (directory / "installed.json").read_text(encoding="utf-8")
            )
            if require_current and (
                metadata.get("pack_version") != self.manifest.pack_version
                or metadata.get("archive_sha256") != self.manifest.archive_sha256
            ):
                return False
            if not all(
                (directory / name).is_file() for name in self.manifest.files.values()
            ):
                return False
            paths = self._compose_paths(
                {"bubble": None, "nickname": None, "title": None}, directory
            )
            self._validate_default_fonts(paths)
            self._load_bundle(
                paths,
                str(metadata.get("pack_version") or directory.name),
            )
            return True
        except (OSError, json.JSONDecodeError, TypeError):
            return False

    def _find_previous_cache(self) -> Path | None:
        if not self.font_root.is_dir():
            return None
        candidates = sorted(
            (
                path
                for path in self.font_root.iterdir()
                if path.is_dir() and not path.name.startswith(".") and path != self.version_dir
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return next(
            (path for path in candidates if self._cache_valid(path, False)), None
        )

    async def _acquire_file_lock(self) -> None:
        token = uuid.uuid4().hex
        while True:
            payload = json.dumps({"pid": os.getpid(), "time": time.time(), "token": token})
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                    lock_file.write(payload)
                self._file_lock_token = token
                return
            except FileExistsError:
                if await asyncio.to_thread(self._remove_stale_lock):
                    continue
                await asyncio.sleep(0.1)

    def _remove_stale_lock(self) -> bool:
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
            stale = time.time() - float(payload["time"]) > 600
            if stale and not self._pid_exists(int(payload["pid"])):
                self.lock_path.unlink(missing_ok=True)
                return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            try:
                if time.time() - self.lock_path.stat().st_mtime > 600:
                    self.lock_path.unlink(missing_ok=True)
                    return True
            except OSError:
                return True
        return False

    def _release_file_lock(self) -> None:
        if self._file_lock_token is None:
            return
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if payload.get("token") == self._file_lock_token:
                self.lock_path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            self._file_lock_token = None

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _inside(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self.font_root)
        except ValueError as exc:
            raise FontVerifyError(f"字体路径逃逸持久化目录: {resolved}") from exc
        return resolved

    def _cleanup_owned_staging(self) -> None:
        for path in tuple(self._owned_staging):
            safe_path = self._inside(path)
            try:
                relative = safe_path.relative_to(self.staging_dir)
            except ValueError as exc:
                raise FontVerifyError(
                    f"拒绝清理字体暂存目录外的路径: {safe_path}"
                ) from exc
            if relative == Path("."):
                raise FontVerifyError("拒绝清理字体暂存根目录")
            if safe_path.is_dir():
                shutil.rmtree(safe_path, ignore_errors=True)
            else:
                safe_path.unlink(missing_ok=True)
            self._owned_staging.discard(path)

    def _fail(self, state: FontState, error: Exception) -> None:
        self._state = state
        self._error = str(error)
        self._ready_event.clear()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
