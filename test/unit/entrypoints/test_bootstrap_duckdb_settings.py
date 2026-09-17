"""Unit tests: DuckDB settings resolved from resolver.yaml and the environment (entrypoints / composition root).

Spec: resolution-resource-bounds — "DuckDB storage is configurable by environment with on-disk default".
Spec: resolution-model-lifecycle — "Training happens once and is frozen" (default threshold).
"""

from pathlib import Path

import pytest
import yaml

from ere.adapters.duckdb_connection import DuckDBStorage
from ere.entrypoints.bootstrap import (
    DuckDBEnvVar,
    resolve_duckdb_settings,
    resolve_model_path,
)
from ere.services.resolver_config import DuckDBConfig, ResolverConfig

SRC_CONFIG_DIR = Path(__file__).parents[3] / "src" / "config"
RESOLVER_CONFIG_FILES = sorted(SRC_CONFIG_DIR.glob("resolver*.yaml"))


def _resolve(config_kwargs: dict, env: dict):
    return resolve_duckdb_settings(DuckDBConfig(**config_kwargs), env)


def test_storage_defaults_to_disk_when_nothing_is_configured():
    settings = _resolve({}, {})

    assert settings.storage == DuckDBStorage.DISK


def test_disk_default_temp_directory_is_beside_database_file():
    settings = _resolve({}, {DuckDBEnvVar.PATH: "/data/app.duckdb"})

    assert settings.path == "/data/app.duckdb"
    assert settings.temp_directory == "/data/app.duckdb.tmp"


@pytest.mark.parametrize(
    "yaml_type, env_storage, expected",
    [
        ("in-memory", None, "memory"),
        ("persistent", None, "disk"),
        ("in-memory", "disk", "disk"),
        ("persistent", "memory", "memory"),
    ],
)
def test_environment_storage_overrides_yaml(yaml_type, env_storage, expected):
    env = {DuckDBEnvVar.STORAGE: env_storage} if env_storage else {}

    settings = _resolve({"type": yaml_type}, env)

    assert settings.storage == expected


def test_limits_and_temp_dir_read_from_environment():
    env = {
        DuckDBEnvVar.STORAGE: "memory",
        DuckDBEnvVar.MEMORY_LIMIT: "2GiB",
        DuckDBEnvVar.THREADS: "2",
        DuckDBEnvVar.TEMP_DIR: "/tmp/ere-spill",
    }

    settings = _resolve({}, env)

    assert settings.memory_limit == "2GiB"
    assert settings.threads == 2
    assert settings.temp_directory == "/tmp/ere-spill"


@pytest.mark.parametrize(
    "variable, value",
    [("STORAGE", "ram"), ("THREADS", "many")],
)
def test_invalid_environment_value_is_rejected_naming_the_variable(variable, value):
    env_var = DuckDBEnvVar[variable]

    with pytest.raises(ValueError, match=str(env_var.value)):
        _resolve({}, {env_var: value})


def test_model_path_defaults_beside_disk_database():
    settings = _resolve({}, {DuckDBEnvVar.PATH: "/data/app.duckdb"})

    assert resolve_model_path(settings, {}) == "/data/app.duckdb.splink_model.json"


def test_model_path_from_environment_wins():
    settings = _resolve({}, {})
    env = {DuckDBEnvVar.MODEL_PATH: "/models/ere.json"}

    assert resolve_model_path(settings, env) == "/models/ere.json"


def test_model_is_not_persisted_for_in_memory_database():
    settings = _resolve({}, {DuckDBEnvVar.STORAGE: "memory"})

    assert resolve_model_path(settings, {}) is None


def test_auto_train_threshold_defaults_to_200():
    config = ResolverConfig.from_dict(
        {
            "threshold": 0.2,
            "match_weight_threshold": -10,
            "top_n": 10,
            "entity_fields": ["legal_name"],
        }
    )

    assert config.auto_train_threshold == 200


@pytest.mark.parametrize("config_file", RESOLVER_CONFIG_FILES, ids=lambda p: p.name)
def test_shipped_resolver_configs_train_at_200(config_file):
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    assert raw["auto_train_threshold"] == 200
