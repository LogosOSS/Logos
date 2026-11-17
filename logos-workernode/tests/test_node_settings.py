"""Node-wide settings: reading, importing, applying."""

from __future__ import annotations

import logging

import pytest

from logos_worker_node import main as worker_main
from logos_worker_node.models import AppConfig, LogosConfig
from logos_worker_node.node_settings import (
    apply_node_settings,
    normalize_node_settings,
    settings_from_yaml,
    settings_from_yaml_file,
    usable,
)


def _cfg(enabled: bool = True) -> AppConfig:
    return AppConfig(logos=LogosConfig(enabled=enabled, logos_url="https://logos.example:8080", shared_key="secret"))


def test_logos_answer_keeps_the_known_settings_and_their_revision():
    body = {"revision": "4", "yaml_imported": 1, "settings": {"gpu_devices": "0", "colour": "red"}}

    assert normalize_node_settings(body) == {"revision": 4, "yaml_imported": True, "settings": {"gpu_devices": "0"}}


@pytest.mark.parametrize("body", [None, [], {"revision": 1}, {"settings": []}])
def test_an_unusable_answer_raises(body):
    with pytest.raises(RuntimeError, match="settings mapping"):
        normalize_node_settings(body)


def test_the_yaml_settings_are_taken_as_written(tmp_path):
    raw = {
        "engines": {"vllm": {"global_extra_args": ["--a=1", 2]}},
        "worker": {"gpu_devices": "0,1", "auto_reboot_on_stuck_gpu": False, "gpu_poll_interval": 7, "port": 80},
    }

    assert settings_from_yaml(raw) == {
        "global_extra_args": ["--a=1", "2"],
        "gpu_devices": "0,1",
        "auto_reboot_on_stuck_gpu": False,
        "gpu_poll_interval": 7,
    }
    assert settings_from_yaml({}) == {}
    assert settings_from_yaml({"engines": "x", "worker": []}) == {}

    path = tmp_path / "config.yml"
    path.write_text("worker:\n  gpu_devices: none\n")
    assert settings_from_yaml_file(path) == {"gpu_devices": "none"}


@pytest.mark.parametrize(
    "key, value, ok",
    [
        ("global_extra_args", ["--a"], True),
        ("global_extra_args", ["--a", ""], False),
        ("global_extra_args", "--a", False),
        ("gpu_devices", "all", True),
        ("gpu_devices", "none", True),
        ("gpu_devices", "0,2", True),
        ("gpu_devices", "x", False),
        ("auto_reboot_on_stuck_gpu", True, True),
        ("auto_reboot_on_stuck_gpu", 1, False),
        ("gpu_poll_interval", 5, True),
        ("gpu_poll_interval", 0, False),
        ("gpu_poll_interval", True, False),
        ("gpu_poll_interval", 301, False),
        ("colour", "red", False),
    ],
)
def test_which_values_the_worker_can_use(key, value, ok):
    assert usable(key, value) is ok


def test_applying_changes_only_what_is_set_and_reports_it():
    cfg = _cfg()
    cfg.worker.gpu_devices = "all"

    changed = apply_node_settings(
        cfg, {"gpu_devices": "all", "global_extra_args": ["--x"], "gpu_poll_interval": 9}, 5
    )

    assert changed == ["global_extra_args", "gpu_poll_interval"]
    assert cfg.engines.vllm.global_extra_args == ["--x"]
    assert cfg.worker.gpu_poll_interval == 9
    assert cfg.worker.gpu_devices == "all"
    assert cfg.logos.node_settings_revision == 5


def test_a_key_the_document_lacks_keeps_the_yaml_value():
    cfg = _cfg()
    cfg.worker.auto_reboot_on_stuck_gpu = False

    assert apply_node_settings(cfg, {}, 2) == []
    assert cfg.worker.auto_reboot_on_stuck_gpu is False


def test_a_value_the_worker_cannot_use_is_skipped_not_fatal(caplog):
    cfg = _cfg()

    changed = apply_node_settings(cfg, {"gpu_devices": "left", "gpu_poll_interval": 8}, 3)

    assert changed == ["gpu_poll_interval"]
    assert cfg.worker.gpu_devices == "all"
    assert "Ignoring gpu_devices" in caplog.text


class _SettingsBridge:
    """Logos side of the first start: empty until something is imported."""

    def __init__(self, stored=None, failures=0, old=False):
        self.stored = stored or {"revision": 0, "yaml_imported": False, "settings": {}}
        self.failures = failures
        self.old = old
        self.fetches = 0
        self.imported: list = []

    async def fetch_node_settings(self):
        self.fetches += 1
        if self.fetches <= self.failures:
            raise RuntimeError("Logos unreachable")
        return None if self.old else self.stored

    async def import_node_settings(self, settings):
        self.imported.append(settings)
        self.stored = {"revision": 1, "yaml_imported": True, "settings": settings}


@pytest.fixture
def _yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    path.write_text("worker:\n  gpu_devices: '0'\n  gpu_poll_interval: 11\n")
    monkeypatch.setenv("LOGOS_WORKER_NODE_CONFIG", str(path))


@pytest.mark.asyncio
async def test_the_first_start_imports_the_yaml_settings_and_reads_them_back(_yaml):
    bridge = _SettingsBridge()
    cfg = _cfg()

    await worker_main._load_central_node_settings(cfg, bridge)  # noqa: SLF001

    assert bridge.imported == [{"gpu_devices": "0", "gpu_poll_interval": 11}]
    assert bridge.fetches == 2
    assert cfg.worker.gpu_devices == "0"
    assert cfg.logos.node_settings_revision == 1


@pytest.mark.asyncio
async def test_stored_settings_win_and_are_not_imported_again(_yaml):
    bridge = _SettingsBridge({"revision": 6, "yaml_imported": True, "settings": {"gpu_devices": "1"}})
    cfg = _cfg()

    await worker_main._load_central_node_settings(cfg, bridge)  # noqa: SLF001

    assert bridge.imported == []
    assert cfg.worker.gpu_devices == "1"
    assert cfg.worker.gpu_poll_interval == 5
    assert cfg.logos.node_settings_revision == 6


@pytest.mark.asyncio
async def test_a_logos_without_settings_leaves_config_yml_in_force(_yaml, caplog):
    caplog.set_level(logging.INFO)
    cfg = _cfg()

    await worker_main._load_central_node_settings(cfg, _SettingsBridge(old=True))  # noqa: SLF001

    assert cfg.worker.gpu_devices == "all"
    assert "config.yml stays in force" in caplog.text


@pytest.mark.asyncio
async def test_the_worker_waits_for_logos_to_answer(_yaml, monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(worker_main.asyncio, "sleep", _no_sleep)
    bridge = _SettingsBridge({"revision": 2, "yaml_imported": True, "settings": {}}, failures=2)

    await worker_main._load_central_node_settings(_cfg(), bridge)  # noqa: SLF001

    assert bridge.fetches == 3


@pytest.mark.asyncio
async def test_with_the_bridge_switched_off_nothing_is_fetched(_yaml):
    bridge = _SettingsBridge()

    await worker_main._load_central_node_settings(_cfg(enabled=False), bridge)  # noqa: SLF001

    assert bridge.fetches == 0
