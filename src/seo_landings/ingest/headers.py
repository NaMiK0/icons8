"""Сопоставление заголовков выгрузки с нашими колонками.

Search Console называет колонки по-разному в зависимости от языка интерфейса
и способа выгрузки («Query», «Top queries», «Запрос»), порядок колонок тоже
не гарантирован. Словарь синонимов держим здесь, а не в загрузчике: добавить
новый язык — правка одной строки.
"""

import re

from ..domain.text import collapse_spaces, strip_accents

QUERY = "query"
CLICKS = "clicks"
IMPRESSIONS = "impressions"
CTR = "ctr"
POSITION = "position"

COLUMNS = (QUERY, CLICKS, IMPRESSIONS, CTR, POSITION)

SYNONYMS: dict[str, tuple[str, ...]] = {
    QUERY: (
        "query",
        "queries",
        "top queries",
        "search query",
        "search queries",
        "keyword",
        "keywords",
        "запрос",
        "запросы",
        "поисковый запрос",
        "самые популярные запросы",
    ),
    CLICKS: ("clicks", "click", "клики", "кликов", "количество кликов"),
    IMPRESSIONS: ("impressions", "impression", "показы", "показов", "количество показов"),
    CTR: ("ctr", "click through rate", "clickthrough rate", "кликабельность"),
    POSITION: (
        "position",
        "average position",
        "avg position",
        "позиция",
        "средняя позиция",
    ),
}

_NOISE = re.compile(r"[()\[\]%.:_\-]+")


def normalize_header(raw: str) -> str:
    """Привести заголовок к сравнимому виду.

    «Average Position (last 28 days)» и «average position» должны совпасть.
    """
    value = strip_accents(str(raw)).replace("﻿", "").casefold()
    value = _NOISE.sub(" ", value)
    return collapse_spaces(value)


def map_columns(header_row: list[str]) -> tuple[dict[str, int], dict[str, str], list[str]]:
    """Сопоставить заголовки с колонками.

    Возвращает (колонка -> индекс, колонка -> исходный заголовок, отсутствующие).
    Сначала ищется точное совпадение с синонимом, затем вхождение синонима
    в заголовок — так «top queries (last 28 days)» тоже находится.
    """
    normalized = [normalize_header(cell) for cell in header_row]
    indexes: dict[str, int] = {}
    found: dict[str, str] = {}
    taken: set[int] = set()

    for column in COLUMNS:
        synonyms = SYNONYMS[column]
        index = _match(normalized, synonyms, taken, exact=True)
        if index is None:
            index = _match(normalized, synonyms, taken, exact=False)
        if index is None:
            continue
        indexes[column] = index
        found[column] = header_row[index].strip()
        taken.add(index)

    missing = [column for column in COLUMNS if column not in indexes]
    return indexes, found, missing


def _match(
    normalized: list[str], synonyms: tuple[str, ...], taken: set[int], *, exact: bool
) -> int | None:
    for index, cell in enumerate(normalized):
        if index in taken or not cell:
            continue
        for synonym in synonyms:
            if cell == synonym if exact else synonym in cell:
                return index
    return None
