"""Чтение .env без внешних зависимостей.

Ключи живут только в окружении: в config.toml их быть не должно, иначе они
рано или поздно уедут в репозиторий. Переменные, уже заданные в окружении,
имеют приоритет над файлом — так проще подменить модель на один прогон.
"""

import os
from pathlib import Path

DEFAULT_ENV_PATH = Path(".env")


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Загрузить .env в os.environ, не затирая уже заданное."""
    source = Path(path) if path else DEFAULT_ENV_PATH
    if not source.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name:
            continue
        loaded[name] = value
        os.environ.setdefault(name, value)
    return loaded
