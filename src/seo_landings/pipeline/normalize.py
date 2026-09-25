"""Нормализация запросов и сборка лексических групп.

Выгрузка полна вариантов одного и того же: «free icons», «free icon»,
«icons free», «freeicon». Это не разные страницы, а один кластер. Лексика
сворачивает очевидные варианты детерминированно и бесплатно — смысловое
объединение остаётся стадии кластеризации, где оно действительно нужно.
"""

import re

from ..domain.models import LexicalGroup, MergedDuplicate, Query, merge_queries
from ..domain.text import collapse_spaces, levenshtein, strip_accents

#: Слова, не меняющие смысла кластера.
STOPWORDS = frozenset(
    {
        "a", "an", "the", "for", "to", "of", "and", "or", "in", "on", "with",
        "my", "your", "best", "top", "online", "com", "www",
    }
)

#: Порог длины ключа, начиная с которого склейка опечаток безопасна.
TYPO_MIN_KEY_LENGTH = 6

_PUNCT_EDGES = re.compile(r"^[^\w]+|[^\w]+$")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_text(raw: str) -> str:
    """Нижний регистр, свёрнутые пробелы, без мусора по краям."""
    value = collapse_spaces(str(raw)).casefold()
    return _PUNCT_EDGES.sub("", value)


def dedupe(queries: list[Query]) -> tuple[list[Query], list[MergedDuplicate]]:
    """Склеить запросы, совпадающие после нормализации.

    «Free Icons» и «free icons» — одна строка в консоли только потому, что
    консоль их различает; для нас это один запрос с суммой метрик.
    """
    buckets: dict[str, list[Query]] = {}
    for query in queries:
        text = normalize_text(query.text)
        buckets.setdefault(text, []).append(query)

    merged: list[Query] = []
    duplicates: list[MergedDuplicate] = []
    for text, group in buckets.items():
        combined = merge_queries(group)
        merged.append(
            combined if combined.text == text else Query(
                raw=combined.raw,
                text=text,
                clicks=combined.clicks,
                impressions=combined.impressions,
                ctr=combined.ctr,
                position=combined.position,
            )
        )
        if len(group) > 1:
            kept = max(group, key=lambda q: (q.clicks, q.impressions))
            duplicates.extend(
                MergedDuplicate(kept=text, absorbed=q) for q in group if q is not kept
            )

    merged.sort(key=lambda q: (-q.clicks, -q.impressions, q.text))
    return merged, duplicates


def lexical_key(text: str) -> str:
    """Сигнатура запроса: отсортированные значимые токены в единственном числе.

    «free icons download», «download free icons» и «icons free download»
    дают один ключ, «icon png» и «png icons» — тоже.
    """
    value = strip_accents(normalize_text(text))
    tokens = [token for token in _NON_WORD.split(value) if token]
    meaningful = [singularize(token) for token in tokens if token not in STOPWORDS]
    if not meaningful:
        meaningful = [singularize(token) for token in tokens]
    return "-".join(sorted(meaningful))


def singularize(token: str) -> str:
    """Грубая, но предсказуемая форма единственного числа."""
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("es") and token[-3:-2] in {"x", "s", "z"}:
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


def build_groups(queries: list[Query], typo_distance: int = 1) -> list[LexicalGroup]:
    """Собрать лексические группы, подклеив опечатки к канону.

    Опечатка («custom curser») — не мусор: это тот же спрос, записанный с
    ошибкой. Она становится вариантом внутри группы, а не отдельной страницей.
    """
    groups: dict[str, LexicalGroup] = {}
    for query in sorted(queries, key=lambda q: (-q.clicks, -q.impressions, q.text)):
        key = lexical_key(query.text)
        target = _match_typo(key, groups, typo_distance) or key
        groups.setdefault(target, LexicalGroup(key=target)).queries.append(query)

    ordered = list(groups.values())
    ordered.sort(key=lambda g: (-g.potential, -g.impressions, -g.clicks, g.key))
    return ordered


def _match_typo(key: str, groups: dict[str, LexicalGroup], distance: int) -> str | None:
    """Найти уже существующий ключ, отличающийся на опечатку."""
    if distance <= 0 or key in groups or len(key) < TYPO_MIN_KEY_LENGTH:
        return None
    for existing in groups:
        if len(existing) < TYPO_MIN_KEY_LENGTH:
            continue
        if levenshtein(key, existing, max_distance=distance) <= distance:
            return existing
    return None


def truncate(queries: list[Query], limit: int | None) -> tuple[list[Query], list[Query]]:
    """Оставить топ-N запросов по потенциалу роста.

    Применяется после склейки дублей: усечение должно считаться по итоговым
    метрикам запроса, а не по его половинкам.
    """
    if not limit or limit <= 0 or len(queries) <= limit:
        return queries, []
    ordered = sorted(queries, key=lambda q: (-q.potential, -q.impressions, -q.clicks, q.text))
    return ordered[:limit], ordered[limit:]
