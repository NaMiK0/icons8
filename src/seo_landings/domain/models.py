"""Модели данных, которыми обмениваются стадии пайплайна.

Слой domain ни от чего не зависит: ни от чтения файлов, ни от LLM, ни от
рендера. Модели добавляются по мере появления стадий (§3 SPEC).
"""

from dataclasses import dataclass, field
from typing import Literal

from . import scoring
from .reasons import Reason

Intent = Literal["transactional", "informational", "navigational"]


@dataclass(frozen=True, slots=True)
class Query:
    """Один поисковый запрос из выгрузки."""

    raw: str
    """Строка запроса как она пришла из CSV."""

    text: str
    """Нормализованная форма: нижний регистр, свёрнутые пробелы."""

    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float | None = None

    @property
    def potential(self) -> float:
        return scoring.potential(self.impressions, self.position)


def merge_queries(queries: list[Query]) -> Query:
    """Склеить дубли одного запроса в один.

    Клики и показы складываются, позиция усредняется с весом показов
    (у запроса с 300k показов позиция значит больше, чем у запроса с 200),
    CTR пересчитывается, а не усредняется.
    """
    if not queries:
        raise ValueError("merge_queries() получил пустой список")
    if len(queries) == 1:
        return queries[0]

    clicks = sum(q.clicks for q in queries)
    impressions = sum(q.impressions for q in queries)

    with_position = [q for q in queries if q.position is not None]
    position: float | None = None
    if with_position:
        weights = [q.impressions for q in with_position]
        total_weight = sum(weights)
        if total_weight > 0:
            position = sum(q.position * q.impressions for q in with_position) / total_weight
        else:
            position = sum(q.position for q in with_position) / len(with_position)

    ctr = clicks / impressions if impressions else 0.0
    primary = max(queries, key=lambda q: (q.clicks, q.impressions))

    return Query(
        raw=primary.raw,
        text=primary.text,
        clicks=clicks,
        impressions=impressions,
        ctr=ctr,
        position=position,
    )


@dataclass(slots=True)
class Decision:
    """Решение по одному запросу: берём или нет, и на каком основании.

    `source` важен не меньше причины: он показывает, кто решил — правило,
    модель или дефолт при неполном ответе модели. Без этого нельзя честно
    ответить, что именно добавила LLM поверх правил.
    """

    query: "Query"
    keep: bool
    reason: "Reason"
    note: str = ""
    source: Literal["rule", "llm", "default", "flag"] = "rule"
    intent: Intent | None = None


@dataclass(slots=True)
class LexicalGroup:
    """Запросы, совпадающие после нормализации: «free icons» и «icons free».

    Группа — не лендинг. Это заготовка, которую стадия кластеризации
    объединяет с другими по смыслу.
    """

    key: str
    """Сигнатура из отсортированных нормализованных токенов."""

    queries: list[Query] = field(default_factory=list)

    @property
    def canonical(self) -> str:
        """Представитель группы — самый «кликабельный» запрос."""
        return max(self.queries, key=lambda q: (q.clicks, q.impressions)).text

    @property
    def clicks(self) -> int:
        return sum(q.clicks for q in self.queries)

    @property
    def impressions(self) -> int:
        return sum(q.impressions for q in self.queries)

    @property
    def potential(self) -> float:
        return sum(q.potential for q in self.queries)

    @property
    def avg_position(self) -> float | None:
        """Средняя позиция, взвешенная показами."""
        ranked = [q for q in self.queries if q.position is not None and q.impressions > 0]
        if not ranked:
            fallback = [q.position for q in self.queries if q.position is not None]
            return sum(fallback) / len(fallback) if fallback else None
        total = sum(q.impressions for q in ranked)
        return sum(q.position * q.impressions for q in ranked) / total


@dataclass(slots=True)
class MergedDuplicate:
    """След склейки дублей — попадает в отчёт с reason=duplicate."""

    kept: str
    absorbed: Query


@dataclass(slots=True)
class Section:
    """Смысловой блок страницы: заголовок H2 и текст под ним."""

    heading: str
    paragraphs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FaqItem:
    question: str
    answer: str


@dataclass(slots=True)
class PageContent:
    """Текстовое наполнение лендинга.

    Источник (`source`) виден в отчёте и в футере страницы: шаблон и модель
    должны быть различимы с первого взгляда, иначе непонятно, что именно
    оценивать.
    """

    title: str
    meta_description: str
    h1: str
    intro: str
    sections: list[Section] = field(default_factory=list)
    faq: list[FaqItem] = field(default_factory=list)
    anchor_text: str = ""
    source: Literal["llm", "template"] = "template"


@dataclass(slots=True)
class Cluster:
    """Группа запросов, под которую делается одна страница."""

    slug: str
    primary_keyword: str
    groups: list[LexicalGroup] = field(default_factory=list)
    intent: Intent = "transactional"
    source: Literal["llm", "lexical"] = "lexical"
    rationale: str = ""
    content: PageContent | None = None

    @property
    def queries(self) -> list[Query]:
        """Все запросы кластера, самые весомые — первыми."""
        collected = [query for group in self.groups for query in group.queries]
        collected.sort(key=lambda q: (-q.clicks, -q.impressions, q.text))
        return collected

    @property
    def clicks(self) -> int:
        return sum(group.clicks for group in self.groups)

    @property
    def impressions(self) -> int:
        return sum(group.impressions for group in self.groups)

    @property
    def potential(self) -> float:
        return sum(group.potential for group in self.groups)

    @property
    def avg_position(self) -> float | None:
        ranked = [q for q in self.queries if q.position is not None and q.impressions > 0]
        if not ranked:
            return None
        total = sum(q.impressions for q in ranked)
        return sum(q.position * q.impressions for q in ranked) / total
