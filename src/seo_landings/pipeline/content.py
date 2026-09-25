"""Текст страницы: шаблонный путь.

Текст собирается из самих запросов кластера по фиксированным формулировкам.
Получается сухо — и это нормально: задача шаблона не заменить копирайт, а
дать структурно полную страницу тогда, когда модель недоступна, и служить
точкой отсчёта, рядом с которой видно, что модель добавляет.

Ограничения длины и запрет на выдуманные факты живут здесь же: стадия LLM
будет проверять свой ответ теми же функциями.
"""

import re

from ..domain.models import Cluster, FaqItem, PageContent, Section

TITLE_LIMIT = 60
DESCRIPTION_LIMIT = 155

#: Слова, по которым в запросах видно формат файла.
FORMATS = ("svg", "png", "ico", "icns", "jpg", "gif", "webp", "pdf", "eps")

#: Слова, по которым видно платформу или сценарий использования.
PLATFORMS = (
    "windows", "mac", "macos", "android", "ios", "linux", "desktop",
    "website", "web", "ppt", "powerpoint", "figma", "app",
)

#: Сокращения, которые в заголовке пишутся заглавными.
ACRONYMS = frozenset(FORMATS + ("ppt", "ui", "ux", "ai", "3d", "html", "css", "ios"))

#: Служебные слова: внутри заголовка остаются строчными.
MINOR_WORDS = frozenset(
    {"a", "an", "the", "for", "to", "of", "and", "or", "in", "on", "with", "by"}
)

_INVENTED_NUMBER = re.compile(r"\d{3,}")


def build_template_content(cluster: Cluster, site_name: str) -> PageContent:
    """Собрать наполнение страницы без обращения к модели."""
    primary = cluster.primary_keyword
    heading = title_case(primary)
    variants = _variants(cluster)
    formats = _found(cluster, FORMATS)
    platforms = _found(cluster, PLATFORMS)

    return PageContent(
        title=clip(f"{heading} | {site_name}", TITLE_LIMIT),
        meta_description=clip(
            f"{heading} for design, web and product work. "
            f"Browse the {primary} collection and pick the format you need.",
            DESCRIPTION_LIMIT,
        ),
        h1=heading,
        intro=(
            f"This page collects everything people look for when they search for {primary}. "
            f"It brings together {len(cluster.queries)} related searches so you can start "
            f"from the wording you already use."
        ),
        sections=[
            Section(
                heading=f"What people mean by {primary}",
                paragraphs=[
                    "Search wording varies, the need does not. "
                    f"The most common variants are {variants}.",
                ],
            ),
            Section(
                heading="Formats and where they fit",
                paragraphs=[_formats_paragraph(formats)],
            ),
            Section(
                heading="Where these assets are used",
                paragraphs=[_platforms_paragraph(platforms, primary)],
            ),
            Section(
                heading="Before you download",
                paragraphs=[
                    "Check the license shown next to an asset before using it, "
                    "especially in commercial work. Licensing differs between "
                    "collections, so read it per asset rather than per page.",
                ],
            ),
        ],
        faq=_faq(cluster, primary),
        anchor_text=primary,
        source="template",
    )


def _variants(cluster: Cluster, limit: int = 5) -> str:
    texts = [query.text for query in cluster.queries[:limit]]
    if len(texts) == 1:
        return f"“{texts[0]}”"
    return ", ".join(f"“{text}”" for text in texts[:-1]) + f" and “{texts[-1]}”"


def _found(cluster: Cluster, vocabulary: tuple[str, ...]) -> list[str]:
    text = " ".join(query.text for query in cluster.queries)
    tokens = set(re.findall(r"[a-z0-9]+", text))
    return [word for word in vocabulary if word in tokens]


def _formats_paragraph(formats: list[str]) -> str:
    if not formats:
        return (
            "Pick a vector format when the asset has to scale, and a raster format "
            "when it goes into a fixed layout. The same asset usually exists in both."
        )

    listed = ", ".join(fmt.upper() for fmt in formats)
    return (
        f"These searches mention {listed}. Vector formats stay sharp at any size and "
        "are the safer default for interfaces; raster formats are simpler to drop into "
        "a fixed layout."
    )


def _platforms_paragraph(platforms: list[str], primary: str) -> str:
    if not platforms:
        return (
            f"Most requests for {primary} come from interface, presentation and "
            "web work, where a consistent set matters more than any single asset."
        )
    listed = ", ".join(platform for platform in platforms)
    return (
        f"The wording around {primary} points at {listed}. Keeping one visual style "
        "across a project reads better than mixing sources."
    )


def _faq(cluster: Cluster, primary: str) -> list[FaqItem]:
    """Вопросы формулируются из реальных запросов кластера."""
    items = [
        FaqItem(
            question=f"Where can I find {primary}?",
            answer=(
                f"Start from the {primary} collection on this page and narrow it down "
                "by style and format."
            ),
        ),
        FaqItem(
            question=f"Are {primary} free to use?",
            answer=(
                "Some assets are free and some are not, and the terms are shown next to "
                "each one. Check the license before using an asset in a published project."
            ),
        ),
        FaqItem(
            question="Which file format should I choose?",
            answer=(
                "Choose a vector format when the asset has to scale or change color, "
                "and a raster format when the size is fixed."
            ),
        ),
    ]

    for query in cluster.queries[1:4]:
        if query.text == primary:
            continue
        items.append(
            FaqItem(
                question=f"What about “{query.text}”?",
                answer=(
                    f"“{query.text}” is part of the same need as {primary}, so this page "
                    "covers it too."
                ),
            )
        )
    return items[:6]


def title_case(value: str) -> str:
    """Заголовок из запроса: «free svg icons for ppt» → «Free SVG Icons for PPT»."""
    words = []
    for index, word in enumerate(value.split()):
        lowered = word.casefold()
        if lowered in ACRONYMS:
            words.append(word.upper())
        elif lowered in MINOR_WORDS and index > 0:
            words.append(lowered)
        else:
            words.append(word[:1].upper() + word[1:])
    joined = " ".join(words)
    return joined[:1].upper() + joined[1:]


def clip(value: str, limit: int) -> str:
    """Обрезать по границе слова — title и description имеют лимиты."""
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    trimmed = value[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return trimmed or value[:limit]


def suspicious_numbers(text: str) -> list[str]:
    """Числа из трёх и более цифр — вероятный выдуманный факт.

    Шаблон таких не создаёт; проверка нужна стадии LLM, где «over 1,000,000
    icons» появляется само собой.
    """
    return _INVENTED_NUMBER.findall(text)
