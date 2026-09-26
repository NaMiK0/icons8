"""Дисковый кэш ответов модели.

Две причины, обе важные. Деньги: повторный прогон на той же выгрузке не
стоит ничего. Воспроизводимость: одни и те же входные данные дают одни и
те же страницы, иначе объяснить результат невозможно — модель отвечает
каждый раз немного по-другому.

Ключ кэша — хэш от модели, промпта и параметров запроса. Меняется что
угодно из этого — запрос уходит в сеть заново.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path(".cache/llm")


def cache_key(payload: dict[str, Any]) -> str:
    """Устойчивый ключ: сортированный JSON без пробелов."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ResponseCache:
    directory: Path = DEFAULT_CACHE_DIR
    enabled: bool = True
    hits: int = 0
    misses: int = 0

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self.directory / f"{key}.json"
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.misses += 1
            return None
        self.hits += 1
        return data

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{key}.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
