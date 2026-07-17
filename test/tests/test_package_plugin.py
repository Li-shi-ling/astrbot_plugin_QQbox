from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = PLUGIN_ROOT / "scripts" / "package_plugin.py"


def load_package_module():
    spec = importlib.util.spec_from_file_location("qqbox_package_plugin", PACKAGE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_read_metadata_name_and_version() -> None:
    package_plugin = load_package_module()

    assert package_plugin.read_metadata_name_and_version() == (
        "astrbot_plugin_QQbox",
        "v1.3.12",
    )


def test_should_package_path_filters_local_and_runtime_files() -> None:
    package_plugin = load_package_module()

    assert package_plugin.should_package_path(Path("main.py")) is True
    assert package_plugin.should_package_path(Path("src/db/repo.py")) is True
    assert package_plugin.should_package_path(Path("AGENTS.md")) is False
    assert package_plugin.should_package_path(Path(".omx/state/runtime.json")) is False
    assert package_plugin.should_package_path(Path("test/tests/test_main.py")) is False
    assert package_plugin.should_package_path(Path(".idea/misc.xml")) is False
    assert package_plugin.should_package_path(Path(".pipeline-workspace/spec.json")) is False
    assert package_plugin.should_package_path(Path("resources/fonts/large.ttf")) is False
    assert package_plugin.should_package_path(Path("avatars/qqbox.db")) is False
    assert package_plugin.should_package_path(Path("avatars/qq_data.json")) is False


def test_package_plugin_writes_install_zip_with_package_root(tmp_path, monkeypatch) -> None:
    package_plugin = load_package_module()
    monkeypatch.setattr(
        package_plugin,
        "list_tracked_files",
        lambda: [
            Path("metadata.yaml"),
            Path("main.py"),
            Path("src/db/repo.py"),
            Path("resources/fonts/large.ttf"),
            Path("test/tests/test_main.py"),
            Path(".idea/misc.xml"),
            Path("avatars/qqbox.db"),
        ],
    )
    output_path = tmp_path / "qqbox.zip"

    result = package_plugin.package_plugin(output_path)

    assert result == output_path.resolve()
    with zipfile.ZipFile(output_path) as archive:
        names = archive.namelist()

    assert names[0] == "astrbot_plugin_QQbox/"
    assert "astrbot_plugin_QQbox/resources/fonts/" in names
    assert "astrbot_plugin_QQbox/metadata.yaml" in names
    assert "astrbot_plugin_QQbox/main.py" in names
    assert "astrbot_plugin_QQbox/src/db/repo.py" in names
    assert "astrbot_plugin_QQbox/resources/fonts/large.ttf" not in names
    assert "astrbot_plugin_QQbox/test/tests/test_main.py" not in names
    assert "astrbot_plugin_QQbox/.idea/misc.xml" not in names
    assert "astrbot_plugin_QQbox/avatars/qqbox.db" not in names
