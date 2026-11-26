"""Tests for repo-ffwd settings schema, coercion, and layered merging."""

import pytest

from repo_ffwd.core.config import get_file_value, load_config, set_config_value
from repo_ffwd.core.errors import ConfigError
from repo_ffwd.core.settings import (
    DEFAULT_EXCLUDES,
    RootConfig,
    Settings,
    allowed_values,
    coerce_value,
    settings_from_layers,
    writable_keys,
)


def test_default_settings_values():
    s = Settings()
    assert s.sync.concurrency == 6
    assert s.sync.timeout_seconds == 60
    assert s.sync.retries == 1
    assert s.sync.prune is True
    assert s.sync.autostash is False
    assert s.sync.include_worktrees is False
    assert s.sync.remote == "origin"
    assert s.defaults.depth == 2
    assert s.defaults.exclude == list(DEFAULT_EXCLUDES)
    assert s.roots == []
    assert s.output.only_changed is True


def test_writable_keys_include_sync_and_defaults_scalars():
    keys = writable_keys()
    assert "sync.concurrency" in keys
    assert "sync.timeout_seconds" in keys
    assert "sync.autostash" in keys
    assert "defaults.depth" in keys
    assert "output.only_changed" in keys
    # List fields and secrets are not scalar writable keys.
    assert "defaults.exclude" not in keys
    assert "roots" not in keys
    assert "token" not in keys


def test_coerce_value_typed_scalars():
    assert coerce_value("sync", "concurrency", "8") == 8
    assert coerce_value("sync", "autostash", "true") is True
    assert coerce_value("sync", "autostash", "no") is False
    assert coerce_value("defaults", "depth", "3") == 3
    assert allowed_values("sync", "autostash") == ("true", "false")

    with pytest.raises(ConfigError):
        coerce_value("sync", "concurrency", "0")
    with pytest.raises(ConfigError):
        coerce_value("sync", "autostash", "maybe")


def test_set_and_reload_typed_config_values(tmp_path):
    cfg_file = tmp_path / "repo_ffwd_config.toml"
    assert set_config_value(cfg_file, "sync.concurrency", "10") == 10
    assert set_config_value(cfg_file, "sync.autostash", "true") is True
    assert get_file_value(cfg_file, "sync.concurrency") == 10
    assert get_file_value(cfg_file, "sync.autostash") is True

    cfg = load_config(config_file=str(cfg_file))
    assert cfg.settings.sync.concurrency == 10
    assert cfg.settings.sync.autostash is True


def test_settings_layers_replace_roots_and_warn_on_invalid():
    layer1 = {
        "sync": {"concurrency": 4, "timeout_seconds": 30},
        "roots": [{"path": "~/first", "depth": 1}],
    }
    layer2 = {
        "sync": {"concurrency": 8},
        "roots": [
            {"path": "~/second", "depth": 3, "tags": ["work"]},
            {"path": ""},  # invalid empty path -> dropped with warning
            "not-a-table",  # invalid type -> dropped with warning
        ],
    }
    settings, warnings = settings_from_layers([layer1, layer2])
    assert settings.sync.concurrency == 8
    assert settings.sync.timeout_seconds == 30
    assert settings.roots == [RootConfig(path="~/second", depth=3, tags=["work"])]
    assert len(warnings) == 2


def test_settings_layers_warn_on_non_list_roots():
    settings, warnings = settings_from_layers([{"roots": "bad"}])
    assert settings.roots == []
    assert any("roots" in w for w in warnings)
