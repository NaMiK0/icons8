"""Чтение config/config.toml.

TOML, а не YAML, сознательно: `tomllib` входит в стандартную библиотеку
Python 3.11+, то есть на чужой машине для запуска не нужно ставить ничего
лишнего. Встроенные дефолты ниже дублируют конфиг, чтобы скрипт работал
даже с потерянным файлом настроек.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config/config.toml")

FLASH = "qwen/qwen3.5-flash-02-23"
QWEN_27B = "qwen/qwen3.5-27b"

DEFAULTS: dict[str, Any] = {
    "run": {"use_llm": True, "use_cache": True, "keep_brand": False, "keep_third_party": False},
    "render": {"show_search_metrics": True},
    "site": {
        "name": "Icons8",
        "base_url": "https://icons8.com/",
        "business": "a catalog of design assets: icons, illustrations and 3D graphics",
    },
    "market": {"language": "en"},
    "brand": {"aliases": ["icons8", "icon8"], "fuzzy_distance": 1, "products": []},
    "input": {"max_queries": 500, "min_impressions": 0},
    "pages": {"target": 10, "tolerance": 2, "min_queries_per_cluster": 2},
    "models": {
        "classify": [FLASH, QWEN_27B],
        "cluster": [QWEN_27B, FLASH],
        "content": [QWEN_27B, FLASH],
    },
    "llm": {"temperature": 0, "max_retries": 4, "batch_size": 50},
    "offline": {"third_party_markers": [], "non_landing_markers": []},
}


class ConfigError(Exception):
    """Конфиг есть, но прочитать его нельзя."""


@dataclass(frozen=True, slots=True)
class Settings:
    data: dict[str, Any]
    path: Path | None = None

    def get(self, dotted: str, default: Any = None) -> Any:
        """Значение по пути вида «pages.target»."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_settings(path: str | Path | None = None) -> Settings:
    """Прочитать конфиг, наложив его поверх встроенных значений."""
    merged = {section: dict(values) for section, values in DEFAULTS.items()}
    source = Path(path) if path else DEFAULT_CONFIG_PATH

    if not source.exists():
        if path:  # путь указали явно — молчать нельзя
            raise ConfigError(f"файл конфигурации не найден: {source}")
        _apply_env_overrides(merged)
        return Settings(data=merged, path=None)

    try:
        with source.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise ConfigError(f"не удалось прочитать {source}: {error}") from error

    for section, values in loaded.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(values)
        else:
            merged[section] = values

    _apply_env_overrides(merged)
    return Settings(data=merged, path=source)


#: Переменные окружения, перекрывающие значения конфига.
ENV_OVERRIDES = {
    "LLM_MODEL_CLASSIFY": ("models", "classify"),
    "LLM_MODEL_CLUSTER": ("models", "cluster"),
    "LLM_MODEL_CONTENT": ("models", "content"),
}


def _apply_env_overrides(data: dict[str, Any]) -> None:
    """Окружение сильнее файла: подменить модель на один прогон — норма.

    Переменная заменяет основную модель стадии; запасные остаются.
    """
    for variable, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(variable)
        if not value:
            continue
        chain = [model for model in _as_list(data.get(section, {}).get(key)) if model != value]
        data.setdefault(section, {})[key] = [value, *chain]


def model_chain(settings: Settings, stage: str) -> list[str]:
    """Модели стадии по порядку: основная, затем запасные."""
    return _as_list(settings.get(f"models.{stage}"))


def _as_list(value: Any) -> list[str]:
    """Модель можно указать строкой или списком — приводим к списку."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item]
